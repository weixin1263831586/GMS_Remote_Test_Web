"""Owner-bound official-reference recall from complete retained evidence text."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import closing

from features.gms import extract_reference_identifiers, recall_gms_requirements, recall_mainline_references
from features.knowledge import partner_policy_allowed, policy_search_terms, recall_web_references
from foundation.error_model import ApiError

from .users import owner_redmine_root


MAX_SCAN_BYTES = 128 * 1024 * 1024
MAX_IDENTIFIERS = 128
_RELEASE = re.compile(r'\bAndroid\s+(1[5-9]|[2-9]\d)\b', re.I)
_BUILD_RELEASE = re.compile(r'ro\.build\.version\.release\s*[:=\]]\s*\[?(1[5-9]|[2-9]\d)\b', re.I)
_BUILD_SDK = re.compile(r'ro\.build\.version\.sdk\s*[:=\]]\s*\[?(35|36|37)\b', re.I)


def collect_snapshot_references(owner_id, snapshot_id, *, query="", data_root=None, allow_partner=False):
    root = owner_redmine_root(owner_id)
    db = root / "evidence.sqlite3"
    if not db.is_file():
        raise ApiError.not_found("证据快照不存在。")
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn, conn:
            conn.row_factory = sqlite3.Row
            snapshot = conn.execute(
                "SELECT issue_id,status,manifest_json FROM redmine_evidence_snapshots WHERE snapshot_id=?", (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise ApiError.not_found("证据快照不存在。")
            if snapshot["status"] in {"queued", "fetching", "downloading"}:
                raise ApiError.conflict("证据快照仍在采集中，请等待快照就绪后再查询官方参考资料。")
            artifacts = conn.execute(
                "SELECT artifact_id,kind,status,derived_status,derived_text_path,stored_path FROM redmine_evidence_artifacts "
                "WHERE snapshot_id=?", (snapshot_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ApiError.dependency_unavailable("证据库暂时不可读，请稍后重试。") from exc
    try:
        manifest = json.loads(snapshot["manifest_json"] or "{}")
    except ValueError as exc:
        raise ApiError.upstream_failure("证据清单格式无效。") from exc
    if not isinstance(manifest, dict):
        raise ApiError.upstream_failure("证据清单格式无效。")
    identifiers = set()
    build_versions = set()
    warnings = []
    scanned = 0
    complete = snapshot["status"] == "ready"
    exhausted = False

    def scan(chunks):
        nonlocal scanned, complete, exhausted
        tail = ""
        for chunk in chunks:
            scanned += len(chunk.encode("utf-8"))
            text = tail + chunk
            build_versions.update(int(value) for value in _BUILD_RELEASE.findall(text))
            build_versions.update(int(value) - 20 for value in _BUILD_SDK.findall(text))
            # 跨分块边界截断的标识符推迟到下一块或 EOF 再确认，
            # 避免把 CVE-2026-5883 + 5 拆成不完整的 CVE。
            identifiers.update(value for value in extract_reference_identifiers(text)
                               if not text.lower().endswith(value.lower()))
            if scanned > MAX_SCAN_BYTES or len(identifiers) > MAX_IDENTIFIERS:
                complete = False
                exhausted = True
                break
            tail = text[-512:]
        identifiers.update(extract_reference_identifiers(tail))

    texts = [query, str(manifest.get("subject") or ""), str(manifest.get("description") or "")]
    texts.extend(str(item.get("notes") or "") for item in manifest.get("journals") or [] if isinstance(item, dict))
    for text in texts:
        if exhausted:
            break
        scan(iter([text]))
    base = (root / "evidence").resolve()
    for artifact in artifacts:
        if exhausted:
            break
        if artifact["status"] != "ready" or artifact["derived_status"] in {"partial", "missing"}:
            complete = False
            warnings.append(f"{artifact['artifact_id']}: 附件下载或文本提取不完整")
        relative = artifact["derived_text_path"] or (
            artifact["stored_path"] if artifact["kind"] in {"text", "log"} else ""
        )
        if not relative:
            if artifact["derived_status"] == "empty":
                continue
            complete = False
            warnings.append(f"{artifact['artifact_id']}: 附件没有可检索文本，识别覆盖不完整")
            continue
        path = (base / relative).resolve()
        if not path.is_relative_to(base):
            complete = False
            warnings.append(f"{artifact['artifact_id']}: 附件文本路径无效")
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                scan(iter(lambda: stream.read(65536), ""))
        except OSError:
            complete = False
            warnings.append(f"{artifact['artifact_id']}: 附件文本不可读")
    result = recall_mainline_references(sorted(identifiers)[:MAX_IDENTIFIERS], data_root=data_root)
    described_versions = {int(value) for value in _RELEASE.findall(" ".join(texts[:3]))}
    versions = described_versions | build_versions
    version = next(iter(versions)) if len(versions) == 1 else None
    policy_identifiers = sorted(identifiers, key=lambda value: (not bool(re.search(r"^(?:Cts|Gts|Mts|Sts|Vts)|#", value, re.I)), value))
    policy_query = " ".join([texts[0], texts[1], *policy_identifiers[:6], texts[2]])[:512]
    policy = recall_web_references(policy_query, android_version=version)
    if policy is not None:
        policy["target_android_version"] = version
        policy["version_resolution"] = "matched" if version is not None else "conflicting" if versions else "unknown"
        result.setdefault("sources", {})["web_documents"] = policy
    if allow_partner and partner_policy_allowed(str(owner_id)):
        result.setdefault('sources', {})['gms_requirements'] = recall_gms_requirements(
            policy_search_terms(policy_query), data_root=data_root
        )
    if exhausted:
        warnings.append("标识符扫描达到资源预算，未扫描部分不能视为无匹配")
    return {"snapshot_id": snapshot_id, "issue_id": snapshot["issue_id"],
            "coverage_complete": complete, "scanned_bytes": scanned, "warnings": warnings,
            **result}


async def append_reference_preflight(owner_id, entry, preflight, progress=None):
    """控制器侧召回，不依赖已安装 MCP 的版本更新。"""
    from .analysis.kkagent.trace import ToolTrace

    if not preflight.snapshot_id:
        return
    entry["_reference_lookup_required"] = True
    tool = "gms_rt_redmine_references"
    arguments = {"snapshot_id": preflight.snapshot_id}
    if progress is not None:
        progress.tool_started(tool, arguments, stage="preflight")
    trace = ToolTrace(tool_call_id="preflight:" + tool, tool_name=tool, tool_input=arguments,
                      snapshot_ids=[preflight.snapshot_id])
    try:
        data = await asyncio.to_thread(collect_snapshot_references, owner_id, preflight.snapshot_id, allow_partner=True)
        if entry.get("issue_id") and str(data["issue_id"]) != str(entry["issue_id"]):
            raise ApiError.not_found("当前工单的证据快照不存在。")
        trace.status = "succeeded"
        trace.reference_lookup = reference_check_summary(data)
        trace.evidence_issue_ids = [data["issue_id"]]
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
        trace.output_sha256 = hashlib.sha256(encoded).hexdigest()
        trace.output_bytes = len(encoded)
        trace.output_preview = "Controller official-reference recall completed"
        entry["_official_reference_context"] = data
    except ApiError as exc:
        trace.status = "failed"
        trace.failure_kind = "reference_unavailable"
        trace.output_preview = exc.message
        entry["_official_reference_context"] = {"status": "unavailable", "reason": exc.message}
    preflight.traces.append(trace)
    if progress is not None:
        progress.tool_finished(tool, None, ok=trace.succeeded, stage="preflight")


async def recover_reference_preflight(entry, trace):
    """Fill the baseline if native fetching recovered after an initial outage."""
    owner = entry.get("_reference_owner_id")
    if not owner or not entry.get("issue_id") or entry.get("_official_reference_context"):
        return
    target = str(entry.get("issue_id") or "")
    snapshots = [snapshot for call in trace.tool_calls
                 if call.succeeded and "redmine_issue_fetch" in call.tool_name
                 and target in {str(call.tool_input.get(key) or "") for key in ("issue", "issue_id")}
                 for snapshot in call.snapshot_ids]
    if not snapshots or any(
        call.succeeded and call.reference_lookup
        and (call.tool_input.get("snapshot_id") in snapshots or set(call.snapshot_ids) & set(snapshots))
        for call in trace.tool_calls if "redmine_references" in call.tool_name
    ):
        return
    from .analysis.kkagent.evidence_preflight import EvidencePreflight

    preflight = EvidencePreflight(snapshot_id=snapshots[-1])
    await append_reference_preflight(owner, entry, preflight, entry.get("_progress_recorder"))
    # Append without renumbering evidence IDs already shown to the model.
    trace.tool_calls.extend(preflight.traces)


def reference_check_summary(data):
    """trace 只保留来源/计数，不落整份上游文档。"""
    sources = data.get("sources") or {}
    return {
        "coverage_complete": data.get("coverage_complete") is True,
        "identifiers": data.get("identifiers") or [],
        "queried_at": data.get("queried_at") or "",
        "sources": {name: {"status": value.get("status"), "count": len(value.get("items") or []),
                           "coverage_complete": value.get("coverage_complete"),
                           "references": [str(item[key]) for item in value.get("items") or []
                                          for key in ("reference", "source_url", "bulletin_url") if item.get(key)]}
                    for name, value in sources.items()},
        "references": list(dict.fromkeys(
            str(item[key]) for source in sources.values() for item in source.get("items") or []
            for key in ("reference", "source_url", "bulletin_url") if item.get(key)
        )),
    }
