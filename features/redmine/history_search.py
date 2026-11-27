"""跨工单历史检索服务（供 /history/search 路由与后续复用）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from features.retrieval import fuse_rankings, local_embedding

from .knowledge.journal_semantic import JournalSemantic
from .users import RESOLVED_STATUS_NAMES
from .utils import search_query_tokens


logger = logging.getLogger(__name__)

# 远端 Redmine 主题搜索的硬超时（秒）：慢站不得拖住整个请求。
REMOTE_SEARCH_TIMEOUT_SECONDS = 15


async def search_issue_history(
    service: Any,
    q: str,
    *,
    limit: int,
    exclude_issue_id: int = 0,
    resolved_only: bool = False,
) -> dict[str, Any]:
    """Rank the owner's archived issues, case facts and journals before remote subject search."""
    items = service.repository.search_history(
        q, exclude_issue_id=exclude_issue_id, limit=limit, resolved_only=resolved_only
    )
    lexical = {int(item["issue_id"]): item for item in items}
    representatives = dict(lexical)
    rankings = [list(lexical)]

    def merge_hits(hits, channel):
        ranking = {}
        for hit in hits:
            issue_id = int(hit["issue_id"])
            if issue_id == exclude_issue_id or issue_id in ranking:
                continue
            item = representatives.get(issue_id)
            if item is None:
                item = _archived_history_item(service.repository, issue_id, resolved_only)
            if item is None:
                continue
            channels = item.get("retrieval_channels", ["lexical"] if issue_id in lexical else [])
            item = {**item, "retrieval_channels": [*channels, channel]}
            if channel == "journal_semantic":
                item["journal_reference"] = {key: hit[key] for key in
                                             ("journal_id", "reference", "source_revision")}
            ranking[issue_id] = item
        representatives.update(ranking)
        rankings.append(list(ranking))

    journal_status = {"status": "disabled", "coverage_complete": False}
    # Share the existing history entry point with MCP/CLI; never send raw reply snippets.
    try:
        embedder = local_embedding("redmine_journals")
        if embedder is not None:
            recalled = await asyncio.to_thread(
                JournalSemantic(service.repository, embedder=embedder).search, q, limit=min(50, limit * 3),
            )
            journal_status = recalled["retrieval"]
            merge_hits(recalled["results"], "journal_semantic")
    except Exception as exc:
        logger.warning("Historical journal retrieval unavailable: %s", type(exc).__name__)
        journal_status = {"status": "error", "coverage_complete": False}
    case_search = getattr(getattr(service, "knowledge", None), "search", None)
    if case_search is not None:
        try:
            cases = await asyncio.to_thread(case_search.search_similar, q, limit=min(50, limit * 3),
                                            exclude_issue_id=exclude_issue_id)
            merge_hits(cases, "case_index")
        except Exception as exc:
            logger.warning("Historical case retrieval unavailable: %s", type(exc).__name__)
    if len(rankings) > 1:
        order = fuse_rankings(rankings, limit=max(1, min(50, limit)))
        items = [representatives[issue_id] for issue_id in order]
    for item in items:
        item["source"] = "local_db"
    seen = {int(item["issue_id"]) for item in items}
    remote_error = ""
    # Local fixes are prioritized; fetch a bounded larger remote candidate pool
    # so de-duplication and status filtering do not silently consume all slots.
    remaining = max(0, min(50, max(1, int(limit)) - len(items)))
    # 远端条目同样标注 matched_terms/distinctive_matches：语料统计来自本地
    # 归档库（DF 是检索语料属性，与单条结果来源无关），命中判定按条目自身
    # 文本。统计不可用时保持字段缺省，消费方按无区分度信息处理。
    token_stats = (
        service.repository.corpus_token_stats(search_query_tokens(q))
        if remaining
        else {}
    )
    if remaining:
        try:
            client = service.agent._make_client()
            try:
                rows = await asyncio.wait_for(
                    client.search_issues_by_subject(
                        q, limit=min(50, max(remaining * 3, remaining)),
                        # Resolved is not necessarily a closed Redmine status.
                        # Apply our resolved-status contract below for all candidates.
                        status_id="*",
                    ),
                    timeout=REMOTE_SEARCH_TIMEOUT_SECONDS,
                )
            finally:
                await client.close()
            for row in rows:
                ref_id = int(row.get("issue_id") or 0)
                if not ref_id or ref_id == exclude_issue_id or ref_id in seen:
                    continue
                # Apply the same resolved-status names to local and remote rows;
                # the Redmine closed flag alone excludes standard Resolved issues.
                if resolved_only and not _status_looks_resolved(row.get("status_name")):
                    continue
                item = {
                    "issue_id": ref_id,
                    "subject": row.get("subject") or "",
                    "status_name": row.get("status_name") or "",
                    "is_resolved": _status_looks_resolved(row.get("status_name")),
                    "summary": "",
                    "solution": "",
                    "patch_direction": "",
                    "fixed_version": "",
                    "category": "",
                    "component": "",
                    "soc_platform": "",
                    "android_version": "",
                    "updated_on": row.get("updated_on") or "",
                    "closed_on": "",
                    "source": "redmine_search",
                }
                if token_stats:
                    service.repository.annotate_match_terms(item, token_stats)
                items.append(item)
                seen.add(ref_id)
                if len(items) >= max(1, int(limit)):
                    break
        except Exception as exc:
            remote_error = str(exc)[:200]
    return {"query": q, "items": items, "remote_search_error": remote_error,
            "retrieval": {"journals": journal_status}, "coverage_complete": False,
            "scope": "bounded_owner_history"}


def _archived_history_item(repository, issue_id, resolved_only):
    issue = repository.get_issue(issue_id)
    if not issue or (resolved_only and not issue.get("is_resolved")):
        return None
    item = {key: issue.get(key) for key in repository.HISTORY_RESULT_FIELDS}
    for key, cap in repository.HISTORY_TEXT_LIMITS.items():
        item[key] = str(item.get(key) or "")[:cap]
    return {**item, "is_resolved": bool(item["is_resolved"]), "matched_terms": [], "distinctive_matches": []}


def _status_looks_resolved(status_name: Any) -> bool:
    text = str(status_name or "").strip().casefold()
    return text in {name.casefold() for name in RESOLVED_STATUS_NAMES}


__all__ = ["search_issue_history"]
