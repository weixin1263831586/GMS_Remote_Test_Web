"""晨报 AI 分析结果 → 本地 case_fact（纯映射，无 I/O）。

Daily Brief 的分析结果经用户明确保存后落进
``redmine_case_facts`` 后即可被 history_search / case_search 的 FTS 检索
到；source_quality 标为 daily_brief_ai，不代表根因已经验证。这里只做字段映射，落库
由 knowledge_repository.upsert_case_fact 负责（upsert 语义：同 issue_id
覆盖、保留 created_at）。
"""

from __future__ import annotations

from typing import Any

from .daily_brief_models import DailyBriefIssue, DailyBriefRun


def _first_line(text: Any, limit: int = 200) -> str:
    value = str(text or "").strip().replace("\r", "")
    line = value.split("\n", 1)[0].strip()
    return line[:limit]


def build_case_fact_from_brief(
    issue_id: int,
    record: DailyBriefIssue,
    run: DailyBriefRun,
) -> dict[str, Any]:
    """把一条 completed 的晨报 issue 记录映射成 case_fact payload。"""
    result = record.result or {}
    evidence = result.get("evidence")
    keywords: list[str] = []
    for item in result.get("similar_issues") or []:
        if isinstance(item, dict) and item.get("subject"):
            keywords.append(str(item["subject"])[:60])
    subject = record.subject or _first_line(result.get("problem_summary"))
    return {
        "issue_id": issue_id,
        "subject": subject,
        "status_name": str(record.status or ""),
        "module": _first_line(result.get("problem_summary"), 40),
        "problem_summary": _first_line(result.get("problem_summary"), 500),
        "root_cause": _first_line(result.get("root_cause"), 500),
        "solution": _first_line(result.get("suggested_solution"), 500),
        "verification": _first_line(
            "；".join(
                str(action.get("action") or "")
                for action in result.get("recommended_actions") or []
                if isinstance(action, dict)
            ),
            500,
        ),
        "reply_template": _first_line(result.get("suggested_reply_zh"), 500),
        "keywords": keywords[:8],
        "evidence": evidence if isinstance(evidence, list) else [],
        "confidence": result.get("confidence") or 0,
        "source_quality": "daily_brief_ai",
        "error_signature": f"daily-brief:{run.brief_date}:{run.run_id}",
    }


__all__ = ["build_case_fact_from_brief"]
