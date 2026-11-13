"""Submit one owner-scoped Redmine analysis and follow its durable run."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from features.redmine import analyze_issue_for_assistant, get_analysis_for_assistant
from foundation.error_model import ApiError
from foundation.redaction import redact_sensitive_data

from .execution_result import ToolResult
from .route_invocation import guarded_tool_call
from .session_log import (
    _append_message,
    _append_step,
    _persist_transcript,
    complete_recovery_step,
)
from .session_registry import (
    AgentRequestShim,
    _agent_monitor_tasks,
    _agent_sessions,
    _now_iso,
    finish_run_state,
    monitor_run_is_current,
    save_session_anchor,
)


logger = logging.getLogger(__name__)
START_TOOL = "redmine_issue_analyze"
STATUS_TOOL = "redmine_issue_analysis_status"
POLL_SECONDS = 2
MAX_TRACKING_SECONDS = 3600
TERMINAL = {"completed", "partial", "failed", "cancelled"}


def _result(response, tool_name):
    payload = json.loads(response.body) if hasattr(response, "body") else response
    if not isinstance(payload, dict):
        raise ValueError("Invalid Redmine analysis response")
    if payload.get("success") is False:
        message = str(payload.get("error") or payload.get("message") or "Redmine 分析请求失败")
        return ToolResult(False, tool_name, data={
            "status_code": getattr(response, "status_code", 400),
            **{key: payload[key] for key in ("code", "next_actions") if key in payload},
        }, error=message, formatted_text=message, page="redmine-agent")
    return ToolResult(True, tool_name, data=payload.get("data", payload), page="redmine-agent")


def _invalid(tool_name, message):
    return _result(ApiError.malformed_request(message).to_response(), tool_name)


def _parameter_error(tool_name, allowed, params):
    message = ("分析请求参数不匹配，任务尚未提交；Agent 需要核对工具参数后重试。"
               if tool_name == START_TOOL else "分析进度查询参数不匹配；Agent 需要核对工具参数后重试。")
    return _result(ApiError.malformed_request(
        message,
        next_actions=({
            "action": "correct_tool_parameters", "tool": tool_name,
            "allowed_parameters": sorted(allowed),
            "unexpected_parameters": sorted(set(params) - allowed) if isinstance(params, dict) else [],
        },),
    ).to_response(), tool_name)


def _current_issue(payload, issue_id=None):
    issues = payload.get("issues") or []
    if issue_id is not None:
        return next((item for item in issues if item.get("issue_id") == issue_id), {})
    return issues[0] if len(issues) == 1 else {}


def _report(payload, issue_id=None):
    issue = _current_issue(payload, issue_id)
    if issue.get("status") != "completed":
        return ""
    result = issue.get("result") or {}
    text = str(result.get("detailed_report") or result.get("problem_summary") or "")
    if result.get("output_style") == "summary":
        return text if text.strip() else ""
    text = text.strip()
    if len(text) > 24000:
        return text[:24000] + "\n\n（报告较长，完整内容请到 Redmine 单号分析查看。）"
    return text


def _summary(payload, issue_id=None):
    run = payload.get("run") or {}
    status = run.get("status")
    issue = _current_issue(payload, issue_id)
    title = f"Redmine #{issue.get('issue_id') or issue_id}" if issue or issue_id else "Redmine 分析"
    label = {"pending": "排队中", "snapshotting": "正在同步工单证据", "analyzing": "正在分析",
             "running": "正在分析", "completed": "分析完成",
             "partial": "部分完成，请关注缺失证据", "failed": "分析失败", "cancelled": "已停止"}.get(status, "等待任务状态")
    text = f"{title}：{label}。"
    if status in {"completed", "partial"}:
        report = _report(payload, issue_id)
        if report and (issue.get("result") or {}).get("output_style") == "summary":
            return report
        text += "\n\n" + (report or "本次任务没有返回该工单的完整分析报告，请在 Redmine 单号分析查看详情。")
    elif status == "failed":
        detail = str(issue.get("error") or run.get("error") or "")[:1000]
        if detail:
            text += "\n" + detail
    return text


async def query_analysis(session, request, params):
    if not isinstance(params, dict) or set(params) - {"run_id"}:
        return _parameter_error(STATUS_TOOL, {"run_id"}, params)
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
        return _invalid(STATUS_TOOL, "请提供提交分析时返回的 run_id。")
    outcome = _result(await get_analysis_for_assistant(request, run_id), STATUS_TOOL)
    if not outcome.success:
        return outcome
    payload = outcome.data
    if not isinstance(payload, dict) or (payload.get("run") or {}).get("run_id") != run_id:
        return _result(ApiError.upstream_failure("分析服务返回的任务标识不匹配。").to_response(), STATUS_TOOL)
    # The page may include an earlier report for continuity. Chat only consumes this run's evidence.
    issues = []
    for issue in payload.get("issues") or []:
        current = {key: value for key, value in issue.items() if key not in {"previous_analysis", "raw_response"}}
        if current.get("status") != "completed":
            current.pop("result", None)
            current.pop("read_model", None)
        issues.append(current)
    outcome.data = {"run": {key: value for key, value in payload["run"].items() if key in {
        "run_id", "status", "mode", "started_at", "finished_at", "error", "cancel_requested",
        "issue_count", "completed_count", "failed_count",
    }}, "issues": issues}
    outcome.formatted_text = _summary(outcome.data)
    return outcome


async def start_analysis(session, request, params):
    allowed = {"issue_ref", "analysis_mode", "analysis_hint"}
    if not isinstance(params, dict) or set(params) - allowed:
        return _parameter_error(START_TOOL, allowed, params)
    if session.get("active_run") or session.get("_redmine_submitting"):
        return _result(ApiError.conflict("当前会话已有运行中的任务，请查询该任务进度，或新建会话分析其他工单。").to_response(), START_TOOL)
    session["_redmine_submitting"] = True
    try:
        outcome = _result(await analyze_issue_for_assistant(
            request, params.get("issue_ref"), params.get("analysis_mode", "incremental"), params.get("analysis_hint", ""),
        ), START_TOOL)
    finally:
        session.pop("_redmine_submitting", None)
    if not outcome.success:
        return outcome
    data = outcome.data
    if not isinstance(data, dict) or not data.get("run_id") or not isinstance(data.get("issue_id"), int):
        return _result(ApiError.upstream_failure("分析服务未返回有效的任务标识；请查询现有任务，避免重复提交。").to_response(), START_TOOL)
    token = str(uuid.uuid4())
    session["status"] = "monitoring"
    session["active_run"] = {
        "redmine_run_id": data["run_id"], "redmine_issue_id": data["issue_id"],
        "started_at": _now_iso(), "monitor_token": token,
    }
    outcome.formatted_text = f"已提交 Redmine #{data['issue_id']} 分析，正在跟踪任务进度。"
    _append_step(session, "提交 Redmine 工单分析", "done", f"工单 #{data['issue_id']} · run_id: {data['run_id']}")
    _append_step(session, f"Redmine #{data['issue_id']} 分析", "running", "等待分析队列处理",
                 {"redmine_run_id": data["run_id"], "redmine_issue_id": data["issue_id"]})
    session["active_run"]["redmine_step_id"] = session["steps"][-1]["id"]
    save_session_anchor(session)
    _append_message(session, "assistant", outcome.formatted_text, data={
        "redmine_run_id": data["run_id"], "redmine_issue_id": data["issue_id"],
    })
    sid = session["session_id"]
    _agent_monitor_tasks[sid] = asyncio.create_task(monitor_redmine_run(sid, AgentRequestShim(request), token))
    return outcome


def _update_step(session, active, status, detail):
    step = next((item for item in session.get("steps", []) if item["id"] == active.get("redmine_step_id")), None)
    if step is None:
        _append_step(session, f"Redmine #{active['redmine_issue_id']} 分析", status, detail,
                     {"redmine_run_id": active["redmine_run_id"]})
        active["redmine_step_id"] = session["steps"][-1]["id"]
        save_session_anchor(session)
    elif (step.get("status"), step.get("detail")) != (status, detail):
        step.update(status=status, detail=redact_sensitive_data(detail), created_at=step.get("created_at") or _now_iso())
        session["updated_at"] = _now_iso()
        _persist_transcript(session)


async def monitor_redmine_run(session_id, request, monitor_token=None):
    session = _agent_sessions.get(session_id)
    if not session or not monitor_run_is_current(session, monitor_token):
        return
    active = session["active_run"]
    run_id, issue_id = active["redmine_run_id"], active["redmine_issue_id"]
    elapsed = 0
    try:
        started = datetime.fromisoformat(active["started_at"])
        elapsed = max(0, (datetime.now(timezone.utc) - started).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    deadline = time.monotonic() + max(0, MAX_TRACKING_SECONDS - elapsed)
    errors = 0
    try:
        while monitor_run_is_current(session, monitor_token):
            if time.monotonic() >= deadline:
                complete_recovery_step(
                    session, active, status="warning", detail="会话重启恢复后等待超时。",
                )
                _update_step(session, active, "warning", "本会话等待超时，后台分析未被取消。")
                finish_run_state(session, "idle")
                _append_message(session, "assistant", f"Redmine #{issue_id} 仍可在单号分析页面查看。任务：{run_id}。")
                return
            outcome = await guarded_tool_call(STATUS_TOOL, "查询 Redmine 分析", query_analysis(session, request, {"run_id": run_id}))
            if not monitor_run_is_current(session, monitor_token):
                return
            if not outcome.success:
                errors += 1
                if errors >= 3 or 400 <= (outcome.data or {}).get("status_code", 500) < 500:
                    complete_recovery_step(
                        session, active, status="warning",
                        detail=f"恢复后跟踪失败：{outcome.error}"[:200],
                    )
                    _update_step(session, active, "warning", outcome.error)
                    finish_run_state(session, "idle")
                    _append_message(session, "assistant", f"无法继续跟踪 Redmine #{issue_id}：{outcome.error}\n任务：{run_id}。后台任务未被重复提交或取消。")
                    return
            else:
                errors = 0
                # 首次成功轮询：后台任务真实状态已确认，收敛重启恢复步骤
                # （done=仍在跟踪中，后续终态由分析步骤自身呈现）。
                complete_recovery_step(session, active)
                payload = outcome.data
                status = payload["run"].get("status")
                if status in TERMINAL:
                    step_status = "done" if status == "completed" and _report(payload, issue_id) else "warning"
                    _update_step(session, active, step_status, _summary(payload, issue_id)[:1000])
                    finish_run_state(session, "idle")
                    _append_message(session, "assistant", _summary(payload, issue_id), data={
                        "redmine_run_id": run_id, "redmine_issue_id": issue_id,
                        "quick_actions": [{"label": "打开 Redmine 看板", "page": "redmine-agent"}],
                    })
                    return
                _update_step(session, active, "running", "排队中" if status == "pending" else "正在取证和分析")
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Redmine analysis monitor failed")
        if monitor_run_is_current(session, monitor_token):
            complete_recovery_step(
                session, active, status="warning", detail="会话恢复后监控异常。",
            )
            _update_step(session, active, "warning", "分析状态跟踪异常，请到 Redmine 单号分析查看。")
            finish_run_state(session, "idle")
            _append_message(session, "assistant", f"分析状态跟踪异常，任务 {run_id} 未被取消，请到 Redmine 单号分析查看。")
    finally:
        if _agent_monitor_tasks.get(session_id) is asyncio.current_task():
            _agent_monitor_tasks.pop(session_id, None)
