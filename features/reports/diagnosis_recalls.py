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
import sqlite3
from typing import Any

from foundation.config import settings
from foundation.redaction import redact_sensitive_text

from .api_helpers import ReportDiagnosisRequest, _get_knowledge_base
from .knowledge_ranking import android_version_from_request, rank_kb_hits


logger = logging.getLogger(__name__)

_MAINLINE_DB_PATH = settings.data_root / 'mainline_known_issues.sqlite3'

BACKGROUND_SOURCE = "android_internals"
BACKGROUND_LIMIT = 6


def query_mainline_exemptions(request: ReportDiagnosisRequest) -> list[dict]:
    """Look up Mainline known-issue exemptions for the failing test.

    Read-only; degrades to an empty list when the DB is absent or the query
    fails (mirrors the graceful degradation of the other recall channels).
    """
    if not _MAINLINE_DB_PATH.exists():
        return []
    from features.system import (
        init_mainline_issues_db,
        query_mainline_exemption_match,
    )

    conn = sqlite3.connect(str(_MAINLINE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        init_mainline_issues_db(conn)
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
        # Relevance dimensions extracted from the failure under diagnosis,
        # used to filter out broad "same-module, wrong-platform/wrong-case"
        # FTS noise (e.g. an RK3399 Android15 ticket surfacing for an
        # RK3576 Android16 SearchView failure).
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
    （knowledge_ranking.android_api_level_from_request）并透传，使
    version-aware rerank 在报告自动诊断这条主入口同样生效。
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
        return list(data.get("results") or [])[:BACKGROUND_LIMIT]
    except Exception as exc:
        logger.warning("System background search failed: %s", redact_sensitive_text(exc))
        return []
