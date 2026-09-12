"""跨工单历史检索服务（供 /history/search 路由与后续复用）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


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
    """合并本地归档库与 Redmine 站内主题搜索的历史检索。

    数据源两部分合并：
    1. 本 owner 的本地归档库（nightly 分析沉淀的 solution/patch_direction）；
    2. Redmine 站内主题搜索（覆盖全部历史工单，含未同步到本库的）。

    本地命中优先（字段更全），远端失败时降级为仅本地结果，
    ``remote_search_error`` 携带降级原因。
    """
    items = service.repository.search_history(
        q, exclude_issue_id=exclude_issue_id, limit=limit, resolved_only=resolved_only
    )
    for item in items:
        item["source"] = "local_db"
    seen = {int(item["issue_id"]) for item in items}
    remote_error = ""
    remaining = max(0, limit - len(items))
    if remaining:
        try:
            client = service.agent._make_client()
            try:
                rows = await asyncio.wait_for(
                    client.search_issues_by_subject(q, limit=remaining),
                    timeout=REMOTE_SEARCH_TIMEOUT_SECONDS,
                )
            finally:
                await client.close()
            for row in rows:
                ref_id = int(row.get("issue_id") or 0)
                if not ref_id or ref_id == exclude_issue_id or ref_id in seen:
                    continue
                items.append({
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
                })
        except Exception as exc:
            remote_error = str(exc)[:200]
    return {"query": q, "items": items, "remote_search_error": remote_error}


def _status_looks_resolved(status_name: Any) -> bool:
    text = str(status_name or "")
    return "关闭" in text or "已解决" in text


__all__ = ["search_issue_history"]
