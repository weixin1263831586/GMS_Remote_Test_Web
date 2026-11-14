"""Redmine 证据快照只读 API。

所有端点：
- 从认证身份推导 owner（禁止任何 owner_id 参数）；
- 显式检查 ``redmine.read`` / ``artifacts.read_own`` / ``apk.analyze_own``；
- 不返回服务端内部路径、异常堆栈或凭据；
- 大内容一律分页/分段。
"""

from __future__ import annotations

import base64
import codecs
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from features.auth import require_agent_scope
from features.redmine.evidence import (
    DOWNLOAD_POLICIES,
    EvidenceError,
    EvidenceFetcher,
    parse_issue_ref,
    snapshot_completeness,
    start_evidence_fetch,
    wait_for_snapshot,
)
from features.redmine.evidence_store import owner_evidence_store
from features.users import owner_id_from_request
from foundation.errors import handle_api_errors


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/redmine-agent")

_IMAGE_MIME_WHITELIST = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}
MCP_IMAGE_MAX_ENCODED_BYTES = 768 * 1024
EVIDENCE_CACHE_TTL_SECONDS = 300
SEARCH_MAX_QUERY_CHARS = 256
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
SNIPPET_CONTEXT_CHARS = 200


def _snapshot_fresh(snapshot: dict[str, Any], ttl_seconds: int) -> bool:
    """快照 fetched_at 距今是否仍在 TTL 内（无法解析时视为不新鲜）。"""
    from datetime import datetime, timedelta

    fetched_at = str(snapshot.get("fetched_at") or "")
    if not fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return datetime.now() - fetched < timedelta(seconds=ttl_seconds)


def _owner(request: Request) -> str:
    return owner_id_from_request(request)


def _get_snapshot(owner_id: str, snapshot_id: str) -> dict[str, Any]:
    store = owner_evidence_store(owner_id)
    snapshot = store.get_snapshot(str(snapshot_id or ""))
    if snapshot is None:
        raise EvidenceError("snapshot 不存在", status_code=404)
    return snapshot


def _get_artifact(owner_id: str, artifact_id: str) -> dict[str, Any]:
    store = owner_evidence_store(owner_id)
    key = str(artifact_id or "").strip()
    artifact = store.get_artifact(key)
    if artifact is None and key.isdigit():
        # 模型偶尔把 Redmine 数字附件 ID 当 art_* 证据 ID 传入；服务端
        # 解析别名，省掉一次往返（2026-09-24 晨报批次真实用例）。
        artifact = store.get_artifact_by_attachment_id(key)
    if artifact is None:
        if key.isdigit():
            raise EvidenceError(
                f"artifact 不存在：'{key}' 是 Redmine 数字附件 ID 而非 art_* 证据 ID，"
                "且当前证据库中没有该附件的映射记录；"
                "请先用 gms-rt-redmine-attachments 获取本 issue 的 art_* 列表",
                status_code=404,
            )
        raise EvidenceError("artifact 不存在", status_code=404)
    snapshot = store.get_snapshot(str(artifact.get("snapshot_id") or ""))
    if snapshot is None:
        raise EvidenceError("artifact 不存在", status_code=404)
    return artifact


def _error_response(exc: EvidenceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": str(exc)},
    )


# ------------------------------------------------------------------ snapshot

@router.post("/issues/{issue_id}/evidence")
@handle_api_errors
async def create_evidence_snapshot(
    issue_id: str,
    request: Request,
):
    """创建（或刷新）一个 issue 的证据快照。"""
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    body: dict[str, Any] = {}
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            body = payload
    except Exception:
        body = {}

    refresh = bool(body.get("refresh", True))
    download = str(body.get("download") or "none")
    if download not in DOWNLOAD_POLICIES:
        return _error_response(
            EvidenceError(f"download 必须是 {DOWNLOAD_POLICIES} 之一", status_code=422)
        )

    from features.redmine.evidence import owner_base_url, preflight_owner_fetch

    try:
        base_url = owner_base_url(owner_id)
        numeric_id = parse_issue_ref(issue_id, base_url)
    except EvidenceError as exc:
        return _error_response(exc)

    store = owner_evidence_store(owner_id)
    if not refresh:
        # refresh=false 只允许复用短 TTL 内的 ready 快照；
        # partial/failed 一律重新抓取。
        existing = store.latest_snapshot_for_issue(numeric_id)
        if (
            existing
            and str(existing.get("status")) == "ready"
            and _snapshot_fresh(existing, EVIDENCE_CACHE_TTL_SECONDS)
            and snapshot_completeness(existing)["derived_text"]
        ):
            return {
                "success": True,
                "data": _snapshot_payload(existing, cache_hit=True),
            }

    # 快照 pre-flight：凭据/地址缺失时在建快照之前
    # 快速失败，不再留 failed 垃圾快照（evidence_fetch.run 内仍保留同一
    # 校验作为后台任务兜底）。
    try:
        preflight_owner_fetch(owner_id)
    except EvidenceError as exc:
        return _error_response(exc)

    fetcher = EvidenceFetcher(owner_id, store=store)
    if bool(body.get("dry_run", False)):
        try:
            await fetcher.probe_issue(numeric_id)
        except EvidenceError as exc:
            return _error_response(exc)
        return {
            "success": True,
            "data": {
                "dry_run": True,
                "issue_id": numeric_id,
                "preconditions": {
                    "base_url_configured": True,
                    "credentials_configured": True,
                    "issue_reference_valid": True,
                },
            },
        }

    snapshot = fetcher.create_snapshot(numeric_id, download=download)
    start_evidence_fetch(owner_id, snapshot)
    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "data": {
                "snapshot_id": snapshot["snapshot_id"],
                "issue_id": numeric_id,
                "status": snapshot.get("status"),
                "status_url": f"/api/redmine-agent/evidence/{snapshot['snapshot_id']}",
            },
        },
    )


def _snapshot_payload(snapshot: dict[str, Any], *, cache_hit: bool = False) -> dict[str, Any]:
    artifacts = None
    data = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "issue_id": snapshot.get("issue_id"),
        "status": snapshot.get("status"),
        "cache_hit": cache_hit,
        "content_sha256": snapshot.get("content_sha256") or "",
        "journal_count": int(snapshot.get("journal_count") or 0),
        "attachment_count": int(snapshot.get("attachment_count") or 0),
        "downloaded_count": int(snapshot.get("downloaded_count") or 0),
        "download_policy": snapshot.get("download_policy") or "none",
        "fetched_at": snapshot.get("fetched_at") or "",
        "source_updated_on": snapshot.get("source_updated_on") or "",
        "errors": snapshot.get("errors") or [],
        "completeness": snapshot.get("completeness") or snapshot_completeness(snapshot, artifacts),
        "complete": False,
    }
    data["complete"] = (
        str(data["status"]) == "ready"
        and all(data["completeness"].values())
        and not data["errors"]
    )
    return data


@router.get("/evidence/{snapshot_id}")
@handle_api_errors
async def get_evidence_status(snapshot_id: str, request: Request):
    require = require_agent_scope("redmine.read")
    require(request)
    try:
        snapshot = _get_snapshot(_owner(request), snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    return {"success": True, "data": _snapshot_payload(snapshot)}


@router.get("/issues/{issue_id}/evidence/latest")
@handle_api_errors
async def get_latest_evidence_snapshot(issue_id: str, request: Request):
    """该 owner 某 issue 的最新快照（issue-show 参数语义）。

    issue-show 收到 issue_id 时用它解析 snapshot_id；优先返回最近 ready
    快照，若只有 running/partial/failed 则返回最近一条并附带其 status，
    便于 CLI 区分「无快照」与「快照未就绪」。
    """
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    from features.redmine.evidence import owner_base_url

    try:
        base_url = owner_base_url(owner_id)
        numeric_id = parse_issue_ref(issue_id, base_url)
    except EvidenceError as exc:
        return _error_response(exc)
    store = owner_evidence_store(owner_id)
    candidates = store.list_snapshots_for_issue(numeric_id, limit=20)
    if not candidates:
        return _error_response(
            EvidenceError(
                f"issue {numeric_id} 还没有证据快照；先运行 "
                f"gms-rt-redmine-issue-fetch {numeric_id}",
                status_code=404,
            )
        )
    ready = [s for s in candidates if str(s.get("status")) == "ready"]
    snapshot = (ready or candidates)[0]
    return {
        "success": True,
        "data": {
            "issue_id": numeric_id,
            "snapshot_id": snapshot.get("snapshot_id"),
            "status": snapshot.get("status"),
            "ready": str(snapshot.get("status")) == "ready",
        },
    }

@router.get("/evidence/{snapshot_id}/wait")
@handle_api_errors
async def wait_evidence_snapshot(
    snapshot_id: str,
    request: Request,
    timeout: int = Query(120, ge=1, le=600),
):
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    try:
        _get_snapshot(owner_id, snapshot_id)
        snapshot = await __import__("asyncio").to_thread(
            wait_for_snapshot, owner_id, snapshot_id, timeout_seconds=timeout
        )
    except EvidenceError as exc:
        return _error_response(exc)
    return {"success": True, "data": _snapshot_payload(snapshot)}


@router.get("/evidence/{snapshot_id}/issue")
@handle_api_errors
async def get_evidence_issue(
    snapshot_id: str,
    request: Request,
    description_offset: int = Query(0, ge=0),
    description_limit: int = Query(65536, ge=1, le=262144),
):
    require = require_agent_scope("redmine.read")
    require(request)
    try:
        snapshot = _get_snapshot(_owner(request), snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    manifest = snapshot.get("manifest") or {}
    description = str(manifest.get("description") or "")
    window = description[description_offset:description_offset + description_limit]
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "issue_id": snapshot.get("issue_id"),
            "subject": manifest.get("subject") or "",
            "status": manifest.get("status") or "",
            "project": manifest.get("project") or "",
            "tracker": manifest.get("tracker") or "",
            "priority": manifest.get("priority") or "",
            "assigned_to": manifest.get("assigned_to") or "",
            "created_on": manifest.get("created_on") or "",
            "updated_on": manifest.get("updated_on") or "",
            "fields": manifest.get("fields") or {},
            "description": {
                "total_chars": len(description),
                "offset": description_offset,
                "returned_chars": len(window),
                "text": window,
                "truncated": description_offset + description_limit < len(description),
            },
        },
    }


@router.get("/evidence/{snapshot_id}/journals")
@handle_api_errors
async def get_evidence_journals(
    snapshot_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    cursor: str = Query(""),
):
    require = require_agent_scope("redmine.read")
    require(request)
    try:
        snapshot = _get_snapshot(_owner(request), snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    journals = (snapshot.get("manifest") or {}).get("journals") or []
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            return _error_response(EvidenceError("cursor 非法", status_code=422))
    window = journals[start:start + limit]
    next_cursor = None
    if start + limit < len(journals):
        next_cursor = str(start + limit)
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "issue_id": snapshot.get("issue_id"),
            "total": len(journals),
            "returned": len(window),
            "next_cursor": next_cursor,
            "truncated": False,
            "journals": window,
        },
    }


@router.get("/evidence/{snapshot_id}/artifacts")
@handle_api_errors
async def get_evidence_artifacts(snapshot_id: str, request: Request):
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    try:
        snapshot = _get_snapshot(owner_id, snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    store = owner_evidence_store(owner_id)
    artifacts = store.list_artifacts(snapshot["snapshot_id"])
    items = []
    for artifact in artifacts:
        items.append({
            "artifact_id": artifact.get("artifact_id"),
            "attachment_id": artifact.get("attachment_id"),
            "filename": artifact.get("filename"),
            "original_filename": artifact.get("original_filename"),
            "content_type": artifact.get("content_type"),
            "detected_content_type": artifact.get("detected_content_type") or "",
            "kind": artifact.get("kind"),
            "status": artifact.get("status"),
            "derived_status": artifact.get("derived_status") or "",
            "text_available": bool(artifact.get("text_available")),
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "declared_size": int(artifact.get("declared_size") or 0),
            "sha256": artifact.get("sha256") or "",
            "error": artifact.get("error") or "",
        })
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "total": len(items),
            "artifacts": items,
        },
    }


# ------------------------------------------------------------------ artifacts

def _artifact_file(owner_id: str, artifact_id: str, *, derived: bool = False) -> tuple[dict[str, Any], Path]:
    artifact = _get_artifact(owner_id, artifact_id)
    store = owner_evidence_store(owner_id)
    # stored_path 已在 _artifact_from_row 中剥离，需要直接查询数据库。
    # 数字附件 ID 别名解析后必须用解析出的 art_* id 查路径，原样字符串
    # 查不到行（2026-09-24 别名回归测试抓到）。
    resolved_id = str(artifact.get("artifact_id") or artifact_id)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT stored_path, derived_text_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
            (resolved_id,),
        ).fetchone()
    if row is None:
        raise EvidenceError("artifact 不存在", status_code=404)
    rel = str(row["derived_text_path"] or "") if derived else str(row["stored_path"] or "")
    if not rel:
        if derived:
            raise EvidenceError(
                "该 artifact 没有可用文本（二进制/扫描件，如 PDF 无文字层）；"
                "不要重复读取，改用 gms-rt-artifact-search 检索其可检索部分，"
                "或在本报告 missing_information 中记录「附件无文本层」"
                "（证据门禁按不可提取处理，不阻塞）",
                status_code=404,
            )
        raise EvidenceError("该 artifact 未下载", status_code=404)
    path = store.resolve_internal(rel)
    if not path.is_file():
        raise EvidenceError("artifact 文件缺失", status_code=404)
    return artifact, path


@router.get("/artifacts/{artifact_id}/download")
@handle_api_errors
async def download_evidence_artifact(artifact_id: str, request: Request):
    require = require_agent_scope("artifacts.read_own")
    require(request)
    try:
        artifact, path = _artifact_file(_owner(request), artifact_id)
    except EvidenceError as exc:
        return _error_response(exc)

    from features.redmine.utils import attachment_content_disposition

    def iterfile():
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        iterfile(),
        media_type=str(artifact.get("content_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": attachment_content_disposition(artifact.get("original_filename")),
            "X-Evidence-SHA256": str(artifact.get("sha256") or ""),
        },
    )


# 文本窗口流式读取的块大小：逐块增量解码并只保留目标窗口，避免把
# 整个（可能数百 MB 的）日志一次性读进内存再切片。
_TEXT_WINDOW_CHUNK_BYTES = 256 * 1024

# zip 索引 sidecar 里最多返回的成员条目：防止成员数巨大的归档把
# 每次窗口读取的响应撑爆。超出时用 member_count/truncated_members 标注。
_ZIP_INDEX_MAX_MEMBERS = 200


def _stream_text_window(path, offset: int, limit: int, encoding: str, errors: str):
    """流式解码计数到 offset 再收集 limit 个字符，随后停止读取。

    内存只持有块级缓冲与窗口内容；越过 ``offset + limit + 1`` 后不再消耗
    磁盘 IO。``total_chars`` 在窗口截断时上报 -1（未知），响应里的
    ``truncated`` 依据窗口是否填满判定——与逐字符精确计数相比，这避免
    为一个统计字段把整个（可能数百 MB 的）日志扫完。

    探测多读一个字符（比较 ``> window_end`` 而非 ``>=``）：文本恰好等于
    窗口长度时，仅读到 ``window_end`` 无法区分"读完恰好收尾"与"后面还有
    内容"，会误报 truncated 让客户端空翻一页；多读一位即可精确判定。
    """
    decoder = codecs.getincrementaldecoder(encoding)(errors)
    total_chars = 0
    collected: list[str] = []
    collecting = offset <= 0
    window_end = offset + limit
    with path.open("rb") as fh:
        while True:
            raw = fh.read(_TEXT_WINDOW_CHUNK_BYTES)
            if not raw:
                tail = decoder.decode(b"", True)
                if tail:
                    total_chars += len(tail)
                    if collecting:
                        collected.append(tail)
                break
            text = decoder.decode(raw)
            total_chars += len(text)
            if collecting:
                collected.append(text)
                if total_chars > window_end:
                    break  # 窗口已填满且确认还有后续内容，提前结束读取。
            elif total_chars >= offset:
                # 本块跨越 offset 边界：只保留边界之后的字符。
                extra = total_chars - offset
                collected.append(text[len(text) - extra:])
                collecting = True
                if total_chars > window_end:
                    break
    window = "".join(collected)[:limit]
    scanned_complete = total_chars <= window_end
    return window, total_chars if scanned_complete else -1


def _stream_tail_window(path, negative_offset: int, limit: int, encoding: str, errors: str):
    """负 offset（从尾部计数）窗口：流式解码整个文件，只保留末尾。

    ``offset=-N`` 表示从最后一个字符向前数 N。读取方由此可以"看日志
    末尾"而无需先知道总长度。deque 只保留 ``-offset + limit`` 个字符，
    内存占用与窗口大小成正比，与文件大小无关；total_chars 始终精确。
    """
    from collections import deque

    keep = deque(maxlen=max(1, -negative_offset + limit))
    decoder = codecs.getincrementaldecoder(encoding)(errors)
    total_chars = 0
    with path.open("rb") as fh:
        while True:
            raw = fh.read(_TEXT_WINDOW_CHUNK_BYTES)
            if not raw:
                tail = decoder.decode(b"", True)
                if tail:
                    total_chars += len(tail)
                    keep.append(tail)
                break
            text = decoder.decode(raw)
            total_chars += len(text)
            if text:
                keep.append(text)
    joined = "".join(keep)
    kept_base = total_chars - len(joined)
    start = max(0, total_chars + negative_offset)
    rel = max(0, start - kept_base)
    return joined[rel:rel + limit], total_chars


def _zip_index_for(path: Path) -> dict[str, Any] | None:
    """读取派生文本旁的 zip 索引 sidecar（旧文件没有则返回 None）。"""
    sidecar = path.parent / f"{path.name}.idx.json"
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    members = payload.get("members") or []
    if not isinstance(members, list):
        return None
    trimmed = []
    for member in members[:_ZIP_INDEX_MAX_MEMBERS]:
        if not isinstance(member, dict):
            continue
        trimmed.append({
            "name": str(member.get("name") or ""),
            "start": int(member.get("start") or 0),
            "length": int(member.get("length") or 0),
        })
    return {
        "total_chars": int(payload.get("total_chars") or 0),
        "member_count": len(members),
        "truncated_members": len(members) > _ZIP_INDEX_MAX_MEMBERS,
        "members": trimmed,
    }


# ------------------------------------------------------------------
# 失败用例表（test_result.xml / 汇总 HTML → 结构化行）
# ------------------------------------------------------------------

_FAILURES_MAX_ROWS = 50
_FAILURES_MAX_MESSAGE_CHARS = 800


def _failure_first_stack(reason: str) -> str:
    """取失败信息里的第一行"栈"：优先异常/断言行，否则首行非空文本。"""
    for line in str(reason or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in ("Exception", "Error", "at ", "Assertion")):
            return stripped[:200]
    return next((line.strip()[:200] for line in str(reason or "").splitlines() if line.strip()), "")


def _failures_from_test_result_xml(text: str, source: str) -> list[dict[str, Any]]:
    """直接解析 test_result.xml（部分附件未走 ReportAnalyzer 管线）。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows: list[dict[str, Any]] = []

    def _visit(node, module_hint=""):
        module = node.get("name") if node.tag in {"Module", "TestModule"} else module_hint
        if node.tag in {"Test", "TestCase"}:
            result = str(node.get("result") or "").lower()
            if result in {"fail", "failed", "error", "assumption_failure"}:
                message = ""
                for child in node:
                    if child.tag in {"Failure", "Error", "StackTrace"}:
                        message = "".join(child.itertext())
                        break
                rows.append({
                    "module": str(node.get("module") or module or module_hint),
                    "test": str(node.get("name") or ""),
                    "result": result,
                    "failure_message": message.strip()[:_FAILURES_MAX_MESSAGE_CHARS],
                    "first_stack_line": _failure_first_stack(message),
                    "source": source,
                })
        for child in node:
            _visit(child, module or module_hint)

    _visit(root)
    return rows[:_FAILURES_MAX_ROWS]


def _failures_from_summary_html(text: str, source: str) -> list[dict[str, Any]]:
    """从 STS/GTS 汇总 HTML 提取失败行：兼容 <table> 行与纯文本 grep。

    ``</tr>`` 转换行、其余标签转空格：汇总 HTML 的失败表一行一个用例，
    展平后保持 "module test FAIL" 同行，才能被行正则命中。
    """
    rows: list[dict[str, Any]] = []
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        stripped = re.sub(r"\s+", " ", line).strip()
        if not stripped or len(stripped) < 8:
            continue
        match = re.match(
            r"^(?P<module>[A-Za-z][\w.]*(?:Cts|Gts|Vts|Sts|CTS|GTS|VTS|STS)[\w.]*)\s+"
            r"(?P<test>[\w#.\[\]]+)\s+(?P<result>FAIL(?:ED)?|ASSUMPTION_FAILURE)\b",
            stripped,
            re.IGNORECASE,
        )
        if not match:
            continue
        key = (match.group("module"), match.group("test"))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "module": match.group("module"),
            "test": match.group("test"),
            "result": match.group("result").lower(),
            "failure_message": stripped[:_FAILURES_MAX_MESSAGE_CHARS],
            "first_stack_line": stripped[:200],
            "source": source,
        })
        if len(rows) >= _FAILURES_MAX_ROWS:
            break
    return rows


def _extract_failure_rows(artifact: dict[str, Any], path: Path) -> tuple[list[dict[str, Any]], str]:
    """按 artifact 类型分派到对应解析器 →（失败行, 解析器名）。

    解析器不可用或格式不匹配时返回空列表（上层给出可操作提示），
    不抛错——失败表提取是提效路径，绝不能变成新的阻塞点。
    """
    name = str(artifact.get("filename") or "").lower()
    if name.endswith(".xml"):
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = _failures_from_test_result_xml(text, "test_result.xml")
        if rows:
            return rows, "xml-parser"
    if name.endswith((".html", ".htm")):
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = _failures_from_summary_html(text, artifact.get("filename"))
        if rows:
            return rows, "summary-html"
        # HTML 里也可能内嵌 test_result XML 结构。
        rows = _failures_from_test_result_xml(text, artifact.get("filename"))
        if rows:
            return rows, "embedded-xml"
        return [], "summary-html"
    if name.endswith(".zip") and "archive" in str(artifact.get("kind") or ""):
        import zipfile

        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    base = Path(info.filename).name.lower()
                    if base == "test_result.xml":
                        rows = _failures_from_test_result_xml(
                            archive.read(info).decode("utf-8", errors="replace"),
                            f"{artifact.get('filename')}!{info.filename}",
                        )
                        if rows:
                            return rows, "zip-test_result.xml"
                # 兜底：zip 内的汇总 HTML。
                for info in archive.infolist():
                    base = Path(info.filename).name.lower()
                    if base.endswith((".html", ".htm")):
                        rows = _failures_from_summary_html(
                            archive.read(info).decode("utf-8", errors="replace"),
                            f"{artifact.get('filename')}!{info.filename}",
                        )
                        if rows:
                            return rows, "zip-summary-html"
        except (zipfile.BadZipFile, OSError):
            return [], "zip"
        return [], "zip"
    return [], "skip"


@router.get("/artifacts/{artifact_id}/text")
@handle_api_errors
async def read_evidence_artifact_text(
    artifact_id: str,
    request: Request,
    offset: int = Query(0),
    limit: int = Query(65536, ge=1, le=262144),
):
    """按字符窗口读取派生文本。

    ``offset >= 0`` 为正向窗口；``offset < 0`` 从尾部计数（-1 = 最后一个
    字符起），此时整个文件会被流式扫描一次以得到精确 total_chars——
    这是显式选择的高成本操作。归档附件若带有派生时的 zip 索引 sidecar，
    响应会附带 ``zip_index``（成员全局 char 区间）与当前窗口所在的
    ``zip_member``/``member_char_index`` 注记，供 search 命中 → 续读
    换算，不再需要手工推算偏移。
    """
    require = require_agent_scope("artifacts.read_own")
    require(request)
    try:
        artifact, path = _artifact_file(_owner(request), artifact_id, derived=True)
    except EvidenceError as exc:
        # 原件存在但无派生文本时，尝试直接按 UTF-8 读取原文件。
        try:
            artifact, path = _artifact_file(_owner(request), artifact_id)
            if str(artifact.get("kind") or "") not in {"text", "log"}:
                raise
        except EvidenceError:
            return _error_response(exc)
    size = path.stat().st_size
    # 流式窗口读取：增量解码并只保留目标窗口，不把整个（可能数百 MB 的）
    # 日志读进内存，也不为统计 total_chars 读完整个文件——窗口填满即停止。
    # total_chars=-1 表示还有后续内容未计数；truncated 同步为窗口语义。
    try:
        if offset >= 0:
            window, total_chars = _stream_text_window(path, offset, limit, "utf-8", "strict")
        else:
            window, total_chars = _stream_tail_window(path, offset, limit, "utf-8", "strict")
    except UnicodeDecodeError:
        if offset >= 0:
            window, total_chars = _stream_text_window(path, offset, limit, "gb18030", "replace")
        else:
            window, total_chars = _stream_tail_window(path, offset, limit, "gb18030", "replace")
    # zip 索引 sidecar：既补全 total_chars（正向窗口扫描提前停止时为 -1），
    # 也把当前窗口映射回其所在的 zip 成员。
    zip_index = _zip_index_for(path)
    if zip_index and total_chars == -1:
        total_chars = zip_index["total_chars"]
    resolved_offset = offset if offset >= 0 else max(0, total_chars + offset)
    data: dict[str, Any] = {
        "artifact_id": artifact.get("artifact_id"),
        "total_chars": total_chars,
        "total_bytes": size,
        "offset": resolved_offset,
        "requested_offset": offset,
        "returned_chars": len(window),
        "text": window,
    }
    if total_chars == -1:
        data["truncated"] = len(window) == limit
    else:
        data["truncated"] = resolved_offset + len(window) < total_chars
    if zip_index:
        data["zip_index"] = zip_index
        member = next(
            (
                m for m in zip_index["members"]
                if m["start"] <= resolved_offset < m["start"] + m["length"]
            ),
            None,
        )
        if member is not None:
            data["zip_member"] = member["name"]
            data["member_char_index"] = resolved_offset - member["start"]
            data["member_char_range"] = [member["start"], member["start"] + member["length"]]
    return {"success": True, "data": data}


@router.get("/artifacts/{artifact_id}/failures")
@handle_api_errors
async def extract_artifact_failures(artifact_id: str, request: Request):
    """解析测试报告附件为结构化失败用例表（服务端一次，所有 agent 受益）。

    支持 test_result.xml（含 zip 内成员）、STS/GTS 汇总 HTML、zip 内
    兜底 HTML。每行给出 module/test/result/failure_message/首行栈/来源，
    agent 不再需要多次窗口往返手工抠失败信息。
    """
    require = require_agent_scope("artifacts.read_own")
    require(request)
    try:
        artifact, path = _artifact_file(_owner(request), artifact_id)
    except EvidenceError as exc:
        return _error_response(exc)
    rows, parser = _extract_failure_rows(artifact, path)
    if rows:
        return {
            "success": True,
            "data": {
                "artifact_id": artifact.get("artifact_id"),
                "filename": artifact.get("filename"),
                "parser": parser,
                "total": len(rows),
                "truncated": len(rows) >= _FAILURES_MAX_ROWS,
                "failures": rows,
            },
        }
    kind = str(artifact.get("kind") or "")
    name = str(artifact.get("filename") or "").lower()
    if parser == "skip":
        return _error_response(EvidenceError(
            "该附件不是测试报告（仅支持 .xml test_result、.html 汇总与 .zip 归档）；"
            "文本类附件请直接 gms-rt-artifact-read 或下载后本地检索",
            status_code=422,
        ))
    if parser in {"summary-html", "zip", "zip-test_result.xml", "zip-summary-html"} or "archive" in kind or name.endswith((".html", ".htm", ".xml")):
        return {
            "success": True,
            "data": {
                "artifact_id": artifact.get("artifact_id"),
                "filename": artifact.get("filename"),
                "parser": parser,
                "total": 0,
                "truncated": False,
                "failures": [],
                "note": (
                    "未提取到结构化失败行：报告格式可能不匹配内置解析器。"
                    "改用 gms-rt-artifact-search 检索 FAIL/stack 关键字，"
                    "或 gms_rt_redmine_attachment_download 落盘后人工解析。"
                ),
            },
        }
    return _error_response(EvidenceError(
        "该附件不是测试报告（仅支持 .xml test_result、.html 汇总与 .zip 归档）",
        status_code=422,
    ))


@router.get("/artifacts/{artifact_id}/image")
@handle_api_errors
async def read_evidence_artifact_image(artifact_id: str, request: Request):
    """返回 MCP image content（base64 原图），超限时明确报错而不是悄悄截断。"""
    require = require_agent_scope("artifacts.read_own")
    require(request)
    try:
        artifact, path = _artifact_file(_owner(request), artifact_id)
    except EvidenceError as exc:
        return _error_response(exc)
    kind = str(artifact.get("kind") or "")
    detected = str(artifact.get("detected_content_type") or "")
    declared = str(artifact.get("content_type") or "").split(";")[0].strip().lower()
    if kind != "image":
        return _error_response(EvidenceError("artifact 不是图片", status_code=422))
    mime = detected if detected in _IMAGE_MIME_WHITELIST else (
        declared if declared in _IMAGE_MIME_WHITELIST else ""
    )
    if not mime:
        suffix = path.suffix.lower()
        mime = next(
            (m for m, ext in _IMAGE_MIME_WHITELIST.items() if ext == suffix), ""
        )
    if not mime:
        return _error_response(
            EvidenceError("图片 MIME 类型无法判定；请下载原件人工查看", status_code=422)
        )
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    if len(encoded) > MCP_IMAGE_MAX_ENCODED_BYTES:
        return _error_response(
            EvidenceError(
                f"图片编码后 {len(encoded)} 字节，超过 MCP 上限 {MCP_IMAGE_MAX_ENCODED_BYTES}；"
                "请下载原件查看",
                status_code=413,
            ),
        )
    return {
        "success": True,
        "data": {
            "artifact_id": artifact.get("artifact_id"),
            "mime_type": mime,
            "base64": encoded,
            "encoding": "base64",
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "sha256": artifact.get("sha256") or "",
            "scaled": False,
            "derived_sha256": artifact.get("sha256") or "",
        },
    }
