"""Report diagnosis 召回通道（从 analysis_api.py 拆出）。

每个召回通道独立失败降级（返回空列表 + warning 日志），互不影响：

- ``query_mainline_exemptions``   Google Mainline known-issue 豁免；
- ``search_knowledge_base``       内部历史案例（issue store + case facts）；
- ``search_system_background``    Android 系统机制背景知识（ADR 0014，
  只经 ``features.knowledge`` 公共面联邦检索，命中恒为 background，
  绝不参与 verified root cause 判定）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from typing import Any

from foundation.config import settings
from foundation.redaction import redact_sensitive_text

from .api_helpers import ReportDiagnosisRequest, _get_knowledge_base


logger = logging.getLogger(__name__)

_MAINLINE_DB_PATH = settings.data_root / 'gms_update.sqlite3'

BACKGROUND_SOURCE = "android_internals"
BACKGROUND_LIMIT = 6


def query_mainline_exemptions(request: ReportDiagnosisRequest) -> list[dict]:
    """Look up Mainline known-issue exemptions for the failing test.

    Read-only; degrades to an empty list when the DB is absent or the query
    fails (mirrors the graceful degradation of the other recall channels).
    """
    if not _MAINLINE_DB_PATH.exists():
        return []
    from features.gms import (
        query_mainline_exemption_match,
    )

    conn = sqlite3.connect(str(_MAINLINE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        from features.gms import init_gms_update_db
        from foundation.database import ensure_schema
        ensure_schema(conn, _MAINLINE_DB_PATH, init_gms_update_db)
        # 匹配器负责校验 test_type。
        # (VTS/unknown → ''), so no separate mapping layer is needed here.
        return query_mainline_exemption_match(
            conn,
            test_module=request.module,
            test_case=request.test_name,
            issue_type=request.test_type,
            limit=10,
        )
    finally:
        conn.close()


# Mainline release notes 的安全补丁表以 CVE 号为主键身份；失败文本
# （错误消息/堆栈）里出现 CVE-YYYY-NNNNN 时按号召回，补齐豁免匹配
# 覆盖不到的「Security patches in this Mainline release」信息。
_CVE_RE = re.compile(r'CVE-\d{4}-\d{4,7}', flags=re.IGNORECASE)


def query_mainline_security_patches(request: ReportDiagnosisRequest) -> list[dict]:
    """Recall security-patch rows by CVE ids found in the failure text."""
    if not _MAINLINE_DB_PATH.exists():
        return []
    cve_ids = _CVE_RE.findall(
        f"{request.error_message or ''}\n{request.stack_trace or ''}"
    )
    if not cve_ids:
        return []
    from features.gms import (
        query_mainline_security_patches as query_patches,
    )

    conn = sqlite3.connect(str(_MAINLINE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        from features.gms import init_gms_update_db
        from foundation.database import ensure_schema
        ensure_schema(conn, _MAINLINE_DB_PATH, init_gms_update_db)
        return query_patches(conn, cve_ids, limit=20)
    finally:
        conn.close()


async def search_mainline_security_patches(request: ReportDiagnosisRequest) -> list[dict]:
    """Async wrapper; a CVE lookup failure degrades to []."""
    try:
        return await asyncio.to_thread(query_mainline_security_patches, request)
    except Exception as exc:
        logger.debug("Mainline security-patch lookup skipped: %s", redact_sensitive_text(exc))
        return []


async def search_mainline_exemptions(request: ReportDiagnosisRequest) -> list[dict]:
    """Async wrapper; a Mainline lookup failure degrades to []."""
    try:
        return await asyncio.to_thread(query_mainline_exemptions, request)
    except Exception as exc:
        logger.debug("Mainline exemption lookup skipped: %s", redact_sensitive_text(exc))
        return []


async def search_knowledge_base(
    http_request: Any,
    request: ReportDiagnosisRequest,
    keywords: list[str],
) -> list[dict]:
    """内部历史案例召回（issue store + case facts），失败降级为 []。"""
    try:
        kb = _get_knowledge_base(http_request)
        kb_query = " ".join(keywords[:5]) or request.test_name or request.error_message[:80]
        if not kb or not kb_query.strip():
            return []
        # Filter recall by test, module and Android version.
        probe = {
            "test_name": request.test_name or "",
            "module": request.module or "",
            "android_version": android_version_from_request(request),
        }
        return await asyncio.to_thread(_gather_kb_hits, kb, kb_query, probe)
    except Exception as kb_error:
        logger.warning("Knowledge base search failed: %s", redact_sensitive_text(kb_error))
    return []


def _gather_kb_hits(kb: Any, kb_query: str, probe: dict[str, str]) -> list[dict]:
    # Two recall channels — the synced issue store (largest, most
    # current) and the curated case_facts — each adapt to the same
    # canonical hit shape via _adapt_hit, so dedup + scoring stay
    # in one place.
    merged: list[dict] = []
    seen: set[int] = set()

    def _adapt(row: dict, source: str, *, issue_store: bool = False) -> None:
        iid = int(row.get("issue_id") or 0)
        if not iid or iid in seen:
            return
        seen.add(iid)
        if issue_store:
            module = row.get("category") or row.get("module") or ""
            root_cause = row.get("error_analysis") or ""
            error_signature = ""
            solution = (row.get("solution") or "")[:600]
        else:
            module = row.get("module") or ""
            root_cause = row.get("root_cause") or ""
            error_signature = row.get("error_signature") or ""
            solution = row.get("solution") or row.get("reply_template") or ""
        merged.append({
            "id": iid,
            "subject": row.get("subject") or "",
            "status_name": row.get("status_name") or "",
            "module": module,
            "chip_platform": row.get("chip_platform") or row.get("soc_platform") or "",
            "android_version": row.get("android_version") or "",
            "error_signature": error_signature,
            "root_cause": root_cause,
            "solution_summary": solution,
            "source": source,
        })

    try:
        repo = getattr(kb, "issue_repository", None)
        if repo is not None:
            for issue in repo.search_similar(kb_query, 0, 20):
                _adapt(issue, "issue_store", issue_store=True)
    except Exception as exc:
        logger.debug("Issue-store KB recall skipped: %s", redact_sensitive_text(exc))
    try:
        for s in kb.search_similar(kb_query, limit=20):
            _adapt(s, "case_facts")
    except Exception as exc:
        logger.debug("Case-facts KB recall skipped: %s", redact_sensitive_text(exc))
    return rank_kb_hits(merged, probe)


async def search_system_background(
    query: str,
    *,
    android_api_level: int | None = None,
) -> list[dict]:
    """Android 系统机制背景知识召回（ADR 0014，background-only）。

    federated_search 走函数内延迟导入：features.knowledge.api 初始化会经
    assistant→reports 链回到本模块，顶层导入会造成循环 import（与
    federation._build_from_config 的延迟手法一致）。联邦层自身已做失败
    隔离；这里再兜底一层，保证该通道任何异常都不影响其余召回与 200 响应。

    ``android_api_level`` 由调用方从报告上下文统一转换
    （android_api_level_from_request）并透传，使
    version-aware rerank 在报告自动诊断这条主入口同样生效。

    Wiki→codesearch 串联溯源（ADR 0014 阶段 ②→③→④）在召回后同步执行：
    每条命中的 source_anchors 逐一去本地 codesearch 检查路径存在性，结论
    内嵌为该 hit 的 ``anchor_verifications``（``evidence_level`` ∈
    path_matched / path_missing / unknown，``path_present`` 为三态布尔）。
    路径命中只证明源码树存在该文件，语义上刻意不叫 verified。
    溯源有预算上限且独立降级，失败不影响背景召回本身，更不影响诊断主流程。
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        from features.knowledge import federated_search

        data = await asyncio.to_thread(
            federated_search,
            query,
            sources=[BACKGROUND_SOURCE],
            limit=BACKGROUND_LIMIT,
            android_api_level=android_api_level,
        )
        results = list(data.get("results") or [])[:BACKGROUND_LIMIT]
        if results:
            results = await _attach_anchor_verifications(
                results, android_api_level=android_api_level
            )
        return results
    except Exception as exc:
        logger.warning("System background search failed: %s", redact_sensitive_text(exc))
        return []


async def _attach_anchor_verifications(
    results: list[dict],
    *,
    android_api_level: int | None = None,
) -> list[dict]:
    """给每条 background 命中内嵌 codesearch 锚点验证结论（独立降级）。

    绑定契约：回挂用本函数生成的结构化 ``hit_id``（命中身份
    投影），绝不按 ``hit_title`` ——两个知识条目同 title 时 title 回挂
    会把验证结论交叉绑定到错误条目。
    """
    try:
        from .anchor_verification import verify_background_anchors

        # 结构化命中 id：就地写入原条目（浅拷贝会让验证结论写到副本而非
        # 调用方条目）。召回结果就地编号即可保证一一对应（验证在同一线程
        # 调用内完成，无并发）。
        for index, hit in enumerate(results):
            if isinstance(hit, dict):
                # provider 结果是不可信证据：不能让其自带的重复
                # hit_id 控制回挂归属。编排层每次都覆盖为局部唯一 id。
                hit["hit_id"] = f"bg-{index}"
        verifications = await asyncio.to_thread(
            verify_background_anchors,
            results,
            android_version=android_version_from_api_level(android_api_level),
        )
    except Exception as exc:
        logger.debug("Anchor verification skipped: %s", redact_sensitive_text(exc))
        return results
    by_hit_id: dict[str, list[dict]] = {}
    for item in verifications:
        hit_id = str(item.get("hit_id") or "")
        if hit_id:
            by_hit_id.setdefault(hit_id, []).append(item)
    for hit in results:
        items = by_hit_id.get(str(hit.get("hit_id") or ""))
        if items:
            hit["anchor_verifications"] = items
    return results


#: Android 大版本 → API level（A13/A14/A15/A16/A17 → 33/34/35/36/37）。
#: 统一 helper：报告诊断、外部知识 version-aware rerank 共用，不要各入口自行猜。
_ANDROID_API_BY_MAJOR = {
    "13": 33,
    "14": 34,
    "15": 35,
    "16": 36,
    "17": 37,
}
_ANDROID_MAJOR_BY_API = {api: major for major, api in _ANDROID_API_BY_MAJOR.items()}

# "android-15" / "Android 16" 形态优先，其次 "16.0_r1" 的前导数字。
_ANDROID_PREFIX_RE = re.compile(r"android[-_.\s]?(\d{1,2})", re.IGNORECASE)
_LEADING_MAJOR_RE = re.compile(r"^(\d{1,2})")


def _android_major(raw: str) -> str:
    text = (raw or "").strip()
    match = _ANDROID_PREFIX_RE.search(text) or _LEADING_MAJOR_RE.match(text)
    return match.group(1) if match else ""


def android_version_from_request(request: ReportDiagnosisRequest) -> str:
    """Best-effort Android major version from a suite or report context."""
    for field in ("suite_version", "android_version"):
        major = _android_major(getattr(request, field, "") or "")
        if major:
            return major
    return ""


def android_api_level_from_request(request: ReportDiagnosisRequest) -> int | None:
    """已知 Android 大版本时返回对应 API level，未知返回 None（跳过版本加权）。"""
    major = android_version_from_request(request)
    return _ANDROID_API_BY_MAJOR.get(major)


def android_version_from_api_level(api_level: int | None) -> str:
    """已知 API level 转回 Android 大版本，未知值返回空串。"""
    try:
        return _ANDROID_MAJOR_BY_API.get(int(api_level), "")
    except (TypeError, ValueError):
        return ""


def test_method_and_class(test_name: str) -> tuple[str, str]:
    """Return method and simple class from ``a.b.C#method`` or ``a.b.C``."""
    test_name = (test_name or "").strip()
    method = ""
    class_name = test_name
    if "#" in test_name:
        class_name, method = test_name.split("#", 1)
    return method.strip(), class_name.rsplit(".", 1)[-1].strip()


def rank_kb_hits(hits: list[dict], probe: dict) -> list[dict]:
    """Filter and re-rank recalled cases against the diagnosed failure."""
    method, simple_class = test_method_and_class(probe.get("test_name") or "")
    module = (probe.get("module") or "").strip()
    probe_android = (probe.get("android_version") or "").strip()

    anchor_platform = ""
    if method:
        for hit in hits:
            if method.lower() in (hit.get("subject") or "").lower():
                anchor_platform = (hit.get("chip_platform") or "").upper()
                if anchor_platform:
                    break

    def score(hit: dict) -> tuple[float, bool]:
        subject = (hit.get("subject") or "").lower()
        root = (hit.get("root_cause") or "").lower()
        searchable = f"{subject} {root}"
        value = 0.0
        case_hit = bool(method and method.lower() in searchable)
        class_hit = bool(simple_class and simple_class.lower() in searchable)
        if case_hit:
            value += 100
        if class_hit:
            value += 40
        if module and module.lower() in searchable:
            value += 15
        if probe_android:
            theirs = (hit.get("android_version") or "").strip()
            if theirs and probe_android in theirs:
                value += 20
        if (hit.get("status_name") or "").lower() in (
            "closed", "confirmed", "已关闭", "已解决", "resolved",
        ):
            value += 10
        if hit.get("source") == "case_facts":
            value += 15
        solution = hit.get("solution_summary") or hit.get("root_cause") or ""
        if len(solution.strip()) >= 40:
            value += 8
        platform = (hit.get("chip_platform") or "").upper()
        keep = not (
            anchor_platform and platform and platform != anchor_platform
            and not (case_hit or class_hit)
        )
        return value, keep

    scored = []
    for hit in hits:
        value, keep = score(hit)
        if not keep:
            continue
        output = dict(hit)
        output["score"] = round(value, 1)
        output["similarity_level"] = (
            "exact" if value >= 100 else "high" if value >= 50
            else "medium" if value >= 30 else "low"
        )
        scored.append((value, output))

    if not scored:
        return [
            {**hit, "score": 0.0, "similarity_level": "low"}
            for hit in hits[:5]
        ]

    scored.sort(key=lambda item: item[0], reverse=True)
    ordered = [hit for _, hit in scored]
    if any(hit["similarity_level"] == "exact" for hit in ordered):
        precise = [
            hit for hit in ordered
            if hit["similarity_level"] in ("exact", "high")
        ]
        supporting = [
            hit for hit in ordered
            if hit["similarity_level"] not in ("exact", "high")
        ]
        return (precise + supporting)[:5]
    return ordered[:5]
