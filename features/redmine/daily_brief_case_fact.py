"""晨报 AI 分析结果 → 本地 case_fact（纯映射，无 I/O）。

审核意见（P1）：本模块早期单独发明了一套 Issue → Case 映射，导致
① 把晨报**执行状态**（completed）写进知识库的 status_name（那不是
Redmine 工单状态）；② project/chip/android/module 等富字段丢失；
③ 覆盖已有高质量知识库记录。修复后：

    Redmine Issue（可选）
        ↓ RedmineCaseExtractor（唯一 Issue→Case 映射来源）
    完整 BaseCaseFact
        ↓ overlay Diagnostic AI fields（根因/方案/验证/回复）
    Knowledge Base

- ``issue`` 为本地扫描库中的工单行时，先由其生成 base fact，再用本次
  **诊断（diagnostic）**分析结论覆盖其中的结论性字段；
- 无 issue 行时退化为纯 AI overlay（status_name 保持空，不写执行状态）；
- 落库由 ``knowledge_repository.upsert_case_fact(..., merge_missing=True)``
  负责：已有非空事实字段不被本次空值清空（non-destructive merge）。
"""

from __future__ import annotations

from typing import Any

from .case_extractor import RedmineCaseExtractor
from .daily_brief_models import DailyBriefIssue, DailyBriefRun


def _first_line(text: Any, limit: int = 200) -> str:
    value = str(text or "").strip().replace("\r", "")
    line = value.split("\n", 1)[0].strip()
    return line[:limit]


# Redmine 事实字段：只来自 RedmineCaseExtractor，绝不取自 AI 结论。
_REDMINE_FACT_KEYS = (
    "subject",
    "status_name",
    "assigned_to_name",
    "project_name",
    "category",
    "chip_platform",
    "android_version",
    "certification_type",
    "module",
    "product_form",
    "region",
)


def build_case_fact_from_brief(
    issue_id: int,
    record: DailyBriefIssue,
    run: DailyBriefRun,
    *,
    issue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一条诊断晨报 issue 记录映射成 case_fact payload。

    ``issue`` 为本地扫描库中的 Redmine 工单行（可选）；提供时其结构化字段
    作为 base，AI 结论仅覆盖结论性字段。
    """
    result = record.result or {}
    base: dict[str, Any] = RedmineCaseExtractor.extract(issue) if issue else {}

    keywords: list[str] = []
    for item in result.get("similar_issues") or []:
        if isinstance(item, dict) and item.get("subject"):
            keywords.append(str(item["subject"])[:60])

    fact: dict[str, Any] = {"issue_id": issue_id}
    # 1) Redmine 事实字段：优先取扫描库提取结果。
    for key in _REDMINE_FACT_KEYS:
        fact[key] = str(base.get(key) or "")
    # subject 允许在无扫描行时退回工单/摘要文本（仍非 AI 杜撰）。
    if not fact["subject"]:
        fact["subject"] = record.subject or _first_line(result.get("problem_summary"))

    # 2) AI 诊断结论 overlay。
    fact["problem_summary"] = _first_line(result.get("problem_summary"), 500) or str(
        base.get("problem_summary") or ""
    )
    fact["root_cause"] = _first_line(result.get("root_cause"), 500)
    fact["solution"] = _first_line(result.get("suggested_solution"), 500)
    fact["verification"] = _first_line(
        "；".join(
            str(action.get("action") or "")
            for action in result.get("recommended_actions") or []
            if isinstance(action, dict)
        ),
        500,
    )
    fact["reply_template"] = _first_line(result.get("suggested_reply_zh"), 500)
    fact["keywords"] = keywords[:8] or list(base.get("keywords") or [])[:8]
    evidence = result.get("evidence")
    fact["evidence"] = evidence if isinstance(evidence, list) and evidence else base.get("evidence") or []
    fact["confidence"] = result.get("confidence") or 0
    # 已有真实错误签名优先；否则用晨报来源标记，便于回溯。
    fact["error_signature"] = str(base.get("error_signature") or "") or (
        f"daily-brief:{run.brief_date}:{run.run_id}"
    )
    fact["source_quality"] = "daily_brief_ai"
    # 保留提取器的症状/文档摘录，供 merge 与质量判断使用。
    if base.get("symptoms"):
        fact["symptoms"] = base["symptoms"]
    if base.get("doc_excerpt"):
        fact["doc_excerpt"] = base["doc_excerpt"]
    return fact


__all__ = ["build_case_fact_from_brief"]
