"""Summary payload and Markdown rendering for a completed Daily Brief run.

聚合三块「执行结果呈现」职责（原 daily_brief_report /
daily_brief_execution_view / daily_brief_execution_statistics，单一消费方
均为 service，按内聚原则并回一处）：run 汇总 + Markdown 渲染、UI 脱敏
issue payload、AI 执行用量统计。
"""

from __future__ import annotations

from typing import Any

from .daily_brief_models import DailyBriefIssue, DailyBriefRun
from .daily_brief_repository import DailyBriefRepository
from .users import _now


DEFAULT_MODEL_LABEL = "kkagent 默认模型（未记录具体名称）"


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
        # 执行完成不等于证据闭环；三个质量桶单独汇总，禁止 UI 用
        # completed 数量冒充“已验证”数量。
        "evidence_verified": sum(
            1 for item in completed
            if (item.result or {}).get("evidence_quality") == "verified"
        ),
        "evidence_partial": sum(
            1 for item in completed
            if (item.result or {}).get("evidence_quality") == "partial"
        ),
        "evidence_insufficient": sum(
            1 for item in completed
            if (item.result or {}).get("evidence_quality") == "insufficient"
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
        f"3 天未回复 "
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
    for issue in sorted(completed, key=lambda item: (-item.priority_score, item.issue_id)):
        result = issue.result or {}
        lines.append(
            f"## {issue.priority} #{issue.issue_id} "
            f"{result.get('problem_summary', '')}"
        )
        # native 深度诊断（kkagent_markdown）没有 triage schema 的
        # customer_request/suggested_solution 字段；按 schema 取值会渲染出
        # 空行和误导性的"置信度：None"（#653167 复盘）。native 格式只输出
        # 模式说明 + 详细分析指引。
        native_summary = result.get("result_format") == "kkagent_markdown"
        if not native_summary:
            lines.append(f"- 客户诉求：{result.get('customer_request', '')}")
            lines.append(f"- 当前阻塞：{result.get('current_blocker') or '未确认'}")
            lines.append(f"- 建议：{result.get('suggested_solution', '')}")
        else:
            lines.append("- 模式：kkagent 深度诊断（无结构化摘要字段）")
        detailed = str(result.get("detailed_report") or "").strip()
        if detailed:
            lines.append("- 详细分析：请在工单详情中查看。")
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
        if native_summary and confidence is None:
            lines.append("- 置信度：未提供（见详细分析）")
        else:
            lines.append(f"- 置信度：{confidence}{review}")
        lines.append("")
    if failed:
        lines.append("## 分析失败")
        lines.extend(f"- #{issue.issue_id}: {issue.error}" for issue in failed)
    return "\n".join(lines)


def _number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def summarize_execution_statistics(
    executions: list[dict[str, Any]],
    *,
    fallback_model: str = "",
) -> dict[str, Any]:
    """Summarize persisted trace metadata without exposing tool inputs/output."""
    fallback = str(fallback_model or "").strip() or DEFAULT_MODEL_LABEL
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
    }
    timing = {
        "total_duration_ms": 0,
        "average_duration_ms": 0,
        "measured_execution_count": 0,
    }
    models: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    issue_ids: set[int] = set()

    for execution in executions:
        if not isinstance(execution, dict):
            continue
        issue_id = _number(execution.get("issue_id"))
        if issue_id:
            issue_ids.add(issue_id)
        model_name = str(execution.get("model_name") or fallback).strip() or fallback
        model = models.setdefault(model_name, {
            "model_name": model_name,
            "execution_count": 0,
            "issue_ids": set(),
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        })
        model["execution_count"] += 1
        if issue_id:
            model["issue_ids"].add(issue_id)

        duration_keys = ("wall_duration_ms", "duration_ms")
        if any(key in execution for key in duration_keys):
            timing["measured_execution_count"] += 1
            timing["total_duration_ms"] += _number(
                execution.get("wall_duration_ms") or execution.get("duration_ms")
            )

        for key in (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens",
        ):
            value = _number(execution.get(key))
            tokens[key] += value
            model[key] += value

        for tool in execution.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            # Controller 侧预检探针（tool_call_id 带 preflight: 前缀）不是
            # agent 的真实调用，不计入工具用量统计。
            if str(tool.get("tool_call_id") or "").startswith("preflight:"):
                continue
            # kkagent 子进程记录的 MCP 工具名是 mcp__<server>__<tool> 形式
            # （如 mcp__gms__gms_rt_redmine_issue_fetch）；归一到 CLI 工具名
            # 后再匹配，否则真实调用会被整体漏计，只剩预检探针的计数。
            tool_name = str(tool.get("tool_name") or "").strip()
            if tool_name.startswith("mcp__"):
                parts = tool_name.split("__", 2)
                tool_name = parts[2] if len(parts) == 3 else tool_name
            if not tool_name.startswith("gms_rt_"):
                continue
            row = tools.setdefault(tool_name, {
                "tool_name": tool_name,
                "call_count": 0,
                "succeeded_count": 0,
                "failed_count": 0,
                "pending_count": 0,
                "issue_ids": set(),
            })
            row["call_count"] += 1
            if issue_id:
                row["issue_ids"].add(issue_id)
            status = str(tool.get("status") or "").lower()
            if status == "succeeded" or (
                not status and not tool.get("is_error")
                and bool(tool.get("output_sha256"))
            ):
                row["succeeded_count"] += 1
            elif status == "failed" or tool.get("is_error"):
                row["failed_count"] += 1
            else:
                row["pending_count"] += 1

    tokens["total_tokens"] = tokens["input_tokens"] + tokens["output_tokens"]
    if timing["measured_execution_count"]:
        timing["average_duration_ms"] = round(
            timing["total_duration_ms"] / timing["measured_execution_count"]
        )
    model_rows = []
    for model in models.values():
        row = {key: value for key, value in model.items() if key != "issue_ids"}
        row["issue_count"] = len(model["issue_ids"])
        row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
        model_rows.append(row)
    model_rows.sort(key=lambda row: (-row["execution_count"], row["model_name"]))

    tool_rows = []
    recommendations = []
    for tool in tools.values():
        row = {key: value for key, value in tool.items() if key != "issue_ids"}
        row["issue_count"] = len(tool["issue_ids"])
        row["failure_rate"] = (
            round(row["failed_count"] / row["call_count"], 4)
            if row["call_count"] else 0
        )
        tool_rows.append(row)
        if row["failed_count"]:
            recommendations.append({
                "tool_name": row["tool_name"],
                "kind": "reliability",
                "message": (
                    f"{row['failed_count']}/{row['call_count']} 次调用失败；"
                    "优先补充失败码、参数校验和重试指引。"
                ),
            })
        elif row["call_count"] >= max(3, row["issue_count"] * 2):
            recommendations.append({
                "tool_name": row["tool_name"],
                "kind": "efficiency",
                "message": (
                    f"覆盖 {row['issue_count']} 个单号却调用 {row['call_count']} 次；"
                    "评估批量查询、结果缓存或提示词去重。"
                ),
            })
    tool_rows.sort(key=lambda row: (-row["call_count"], row["tool_name"]))
    recommendations.sort(key=lambda row: (row["kind"] != "reliability", row["tool_name"]))

    return {
        "execution_count": len(executions),
        "issue_count": len(issue_ids),
        "tokens": tokens,
        "timing": timing,
        "models": model_rows,
        "gms_tool_call_count": sum(row["call_count"] for row in tool_rows),
        "gms_tools": tool_rows,
        "tool_improvement_recommendations": recommendations[:8],
    }


def _tool_succeeded(tools: list[Any], name_fragment: str) -> bool:
    """Accept current traces and the previous persisted trace shape."""
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("tool_name") or "")
        has_result = tool.get("status") == "succeeded" or (
            not tool.get("status")
            and not tool.get("is_error")
            and bool(tool.get("output_sha256"))
        )
        if has_result and name_fragment in name:
            return True
    return False


def _tool_status(tools: list[Any], name_fragment: str) -> str:
    """Return a presentation-safe evidence state from the latest trace."""
    matched = [
        tool for tool in tools
        if isinstance(tool, dict) and name_fragment in str(tool.get("tool_name") or "")
    ]
    if not matched:
        return "not_collected"
    if any(_tool_succeeded([tool], name_fragment) for tool in matched):
        return "succeeded"
    failure_kinds = {
        str(tool.get("failure_kind") or "") for tool in matched
        if str(tool.get("failure_kind") or "")
    }
    for status in ("service_unavailable", "invalid_request", "device_unavailable"):
        if status in failure_kinds:
            return status
    if any(str(tool.get("status") or "").lower() == "failed" for tool in matched):
        return "unavailable"
    return "collecting"


def issue_payload(
    issue: DailyBriefIssue,
    execution: dict[str, Any] | None = None,
    execution_statistics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the browser payload without raw model or tool output."""
    payload = issue.to_row()
    payload.pop("raw_response", None)
    if execution_statistics:
        payload["ai_statistics"] = execution_statistics
    # DiagnosisReadModel（全局审查第二十节）：canonical 诊断读模型，供
    # CLI/MCP/Assistant 与 Web 按同一种形状消费；Web 的完整展示字段
    # （suggested_reply_* 等）仍在原生 result 里。
    try:
        from .diagnosis_read_model import read_model_from_issue_result

        read_model = read_model_from_issue_result(
            issue.result, issue_id=issue.issue_id,
            subject=issue.subject, execution=execution,
        )
        if read_model is not None:
            payload["read_model"] = read_model
    except Exception:
        # 投影是纯增量展示能力，任何异常都不影响既有 payload 返回。
        pass
    if not execution:
        return payload

    tools = execution.get("tools") or []
    gate = (issue.result or {}).get("evidence_gate") or {}
    failure_stage = str(
        execution.get("failure_stage") or execution.get("error_type") or ""
    )
    final_ok = bool(execution.get("final_ok"))
    schema_status = (
        "failed"
        if failure_stage == "schema_mismatch"
        else "passed"
        if final_ok or failure_stage == "evidence_gate_failed"
        else "unknown"
    )
    payload["ai_execution"] = {
        "session_id": execution.get("session_id") or "",
        "status": execution.get("status") or "",
        "failure_stage": failure_stage,
        "failure_message": execution.get("failure_message") or "",
        "schema_status": schema_status,
        "issue_fetched": _tool_succeeded(tools, "redmine_issue_fetch"),
        "device_evidence_status": (
            # 终态失败的 execution 里残留 pending 取证 trace 只说明进程在
            # 取证完成前死亡；"collecting" 仅对仍在运行的 run 有意义，
            # 否则 UI 会把已结束的 run 永久标成"实机取证中"。
            "unavailable"
            if (_tool_status(tools, "gms_rt_devices_snapshot") == "collecting" and not final_ok)
            else _tool_status(tools, "gms_rt_devices_snapshot")
        ),
        "journals_checked": _tool_succeeded(tools, "redmine_journals"),
        "attachments_checked": gate.get("attachments_checked") is True,
        "source_evidence_checked": bool(
            execution.get("source_evidence_checked")
        ) or _tool_succeeded(tools, "gms_rt_sdk_") or _tool_succeeded(
            tools, "gms_rt_apk_"
        ),
        "source_evidence_tool_count": int(
            execution.get("source_evidence_tool_count") or 0
        ),
        "history_search_count": int(execution.get("history_search_count") or 0),
        "distinct_history_search_count": int(
            execution.get("distinct_history_search_count") or 0
        ),
        "tool_call_count": int(execution.get("tool_call_count") or 0),
        "repair_attempts": int(execution.get("repair_attempts") or 0),
        "duration_ms": int(
            execution.get("wall_duration_ms")
            or execution.get("duration_ms")
            or 0
        ),
        "input_tokens": int(execution.get("input_tokens") or 0),
        "output_tokens": int(execution.get("output_tokens") or 0),
        "recorded_at": execution.get("recorded_at") or "",
    }
    return payload


__all__ = [
    "DEFAULT_MODEL_LABEL",
    "issue_payload",
    "render_daily_brief_markdown",
    "summarize_daily_brief",
    "summarize_execution_statistics",
]
