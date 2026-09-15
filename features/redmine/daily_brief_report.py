"""Summary payload and Markdown rendering for a completed Daily Brief run."""

from __future__ import annotations

from typing import Any

from .daily_brief_models import DailyBriefIssue, DailyBriefRun
from .daily_brief_repository import DailyBriefRepository
from .users import _now


def summarize_daily_brief(
    repository: DailyBriefRepository,
    run: DailyBriefRun,
    snapshot_entries: dict[int, dict[str, Any]],
) -> DailyBriefRun:
    """Build and persist the aggregate report after per-issue analysis."""
    issues = repository.list_issues(run.run_id)
    completed = [item for item in issues if item.status == "completed"]
    failed = [item for item in issues if item.status == "failed"]
    counts = {
        "total": len(issues),
        "waiting_my_reply": run.waiting_my_reply_count,
        "no_reply_3_days": run.no_reply_3_days_count,
        "completed": len(completed),
        "failed": len(failed),
        "needs_human_review": sum(
            1
            for item in completed
            if bool((item.result or {}).get("needs_human_review"))
        ),
    }
    top = [
        {
            "issue_id": item.issue_id,
            "priority": item.priority,
            "subject": (
                (snapshot_entries.get(item.issue_id) or {}).get("subject")
                or item.subject
            ),
            "problem_summary": (item.result or {}).get("problem_summary", ""),
            "confidence": (item.result or {}).get("confidence"),
        }
        for item in sorted(completed, key=lambda value: -value.priority_score)[:5]
    ]
    run.report_json = {
        "brief_date": run.brief_date,
        "generated_at": _now(),
        "counts": counts,
        "top_priorities": top,
        "snapshot_hash": run.snapshot_hash,
        "source_sync_status": run.source_sync_status,
        # 数据质量与执行状态是两个维度：execution status（本 run 的
        # status 字段）描述 AI 分析；data_quality 描述输入数据可信度。
        "data_quality": run.data_quality,
        "last_sync_at": run.last_sync_at,
        "analysis_backend": run.analysis_backend,
        "model": run.model_name,
    }
    run.report_markdown = render_daily_brief_markdown(run, completed, failed)
    if failed and not completed:
        run.status = "failed"
        run.error = f"{len(failed)} issue analyses failed"
    elif failed or len(completed) < len(issues):
        run.status = "partial"
        run.error = "" if completed else "all issue analyses failed"
    else:
        run.status = "completed"
        run.error = ""
    run.finished_at = _now()
    repository.update_run(run)
    return run


def render_daily_brief_markdown(
    run: DailyBriefRun,
    completed: list[DailyBriefIssue],
    failed: list[DailyBriefIssue],
) -> str:
    """Render the persisted human-readable Daily Brief report."""
    lines = [f"# Redmine 每日晨报 {run.brief_date}", ""]
    counts = run.report_json.get("counts", {})
    lines.append(
        f"共 {counts.get('total', 0)} 个待处理（待回复 "
        f"{counts.get('waiting_my_reply', 0)} / 超 "
        f"{run.no_reply_3_days_count and ''}3 天未回复 "
        f"{counts.get('no_reply_3_days', 0)}），成功分析 "
        f"{counts.get('completed', 0)}，失败 {counts.get('failed', 0)}。"
    )
    if run.source_sync_status in ("sync_failed", "skipped"):
        lines.append(
            f"> 注意：快照源同步状态为 {run.source_sync_status}，"
            "本报告基于本地镜像（数据可能落后于 Redmine）。"
        )
    elif run.data_quality == "stale":
        lines.append(
            "> 注意：本地数据已超过 24 小时未更新（data_quality=stale），"
            "结论可能基于过期信息。"
        )
    lines.append("")
    for issue in completed:
        result = issue.result or {}
        lines.append(
            f"## {issue.priority} #{issue.issue_id} "
            f"{result.get('problem_summary', '')}"
        )
        lines.append(f"- 客户诉求：{result.get('customer_request', '')}")
        lines.append(
            f"- 根因（{result.get('root_cause_type', 'unknown')}）："
            f"{result.get('root_cause', '')}"
        )
        lines.append(f"- 建议：{result.get('suggested_solution', '')}")
        detailed = str(result.get("detailed_report") or "").strip()
        if detailed:
            lines.extend(["", "### 详细分析报告", detailed, ""])
        similar = result.get("similar_issues") or []
        if similar:
            refs = "；".join(
                f"#{item.get('issue_id')}（{item.get('similarity', 'related')}）"
                + (
                    f"：{item.get('reusable_fix')}"
                    if item.get("reusable_fix")
                    else ""
                )
                for item in similar
            )
            lines.append(f"- 相似工单：{refs}")
        elif not result.get("history_checked"):
            lines.append("- 相似工单：未检索（历史库不可用或未执行）")
        confidence = result.get("confidence")
        review = "（需人工确认）" if result.get("needs_human_review") else ""
        lines.extend([f"- 置信度：{confidence}{review}", ""])
    if failed:
        lines.append("## 分析失败")
        lines.extend(f"- #{issue.issue_id}: {issue.error}" for issue in failed)
    return "\n".join(lines)


__all__ = ["render_daily_brief_markdown", "summarize_daily_brief"]
