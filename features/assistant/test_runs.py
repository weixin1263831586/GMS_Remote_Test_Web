"""Start, prepare, and monitor one Agent test plan."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import Request

from features.assistant.executor import _json_body
from features.assistant.failure_analysis import (
    _latest_report_for_client,
    _report_failed,
    _report_for_cluster_job,
    _run_failure_analysis_pipeline,
)
from features.assistant.payloads import jsonable as _jsonable
from features.assistant.plan_submission import execute_plan_start, superseded_plan_result
from features.assistant.session_log import _append_message, _append_step
from features.assistant.session_registry import (
    AgentRequestShim,
    _agent_monitor_tasks,
    _agent_sessions,
    _now_iso,
    owner_id_from_request_shim,
)
from features.assistant.session_registry import (
    finish_run_state as _finish_run_state,
)
from features.assistant.session_registry import (
    monitor_run_is_current as _monitor_run_is_current,
)
from features.assistant.session_registry import (
    save_session_anchor as _save_session_anchor,
)
from features.assistant.workspace import _is_local_worker_id, _local_worker_id
from features.cluster import (
    ACTIVE_CLUSTER_JOB_STATUSES as _ACTIVE_CLUSTER_JOB_STATUSES,
)
from features.devices import get_or_create_user_state
from features.users import owner_id_from_request
from foundation.error_model import record_internal_error


logger = logging.getLogger(__name__)

MAX_AGENT_RETRIES = 3


async def _start_test_with_plan(session: dict[str, Any], request_shim: AgentRequestShim, plan: dict[str, Any], retry_timestamp: str = "") -> dict[str, Any]:
    from features.test_execution import TestStartRequest, start_test

    active_run = session.get("active_run")
    req_data = dict(plan.get("request") or {})
    if retry_timestamp:
        req_data["retry_dir"] = retry_timestamp
        req_data["test_module"] = ""
        req_data["test_case"] = ""
    # 跨重启幂等：plan.operation_id 以 "agent-op:" 前缀走 automation_run_id
    # 通道（集群仓库持久化 + 唯一查询）：任务已创建而响应未达时，同 ID
    # 重试返回既有任务；retry 复用任务目录，不注入。
    operation_id = str(plan.get("operation_id") or "")
    if operation_id and not retry_timestamp and not req_data.get("automation_run_id"):
        req_data["automation_run_id"] = f"agent-op:{operation_id}"
    req = TestStartRequest(**req_data)
    response = await start_test(request_shim, help=False, req=req)
    payload = _json_body(response)
    if session.get("active_run") is not active_run:
        return superseded_plan_result(payload)
    if payload.get("success"):
        correlation = payload.get("data") or {}
        _append_step(
            session,
            "启动测试" if not retry_timestamp else "启动 retry",
            "running",
            retry_timestamp or req.test_module or req.test_suite,
            {"request": req.model_dump(), "correlation": correlation},
        )
        if correlation.get("cluster_job_id"):
            active_run = session.setdefault("active_run", {})
            resolved_worker_id = correlation.get("worker_id") or req.worker_id
            active_run.update({
                "cluster_job_id": correlation["cluster_job_id"],
                "attempt_id": correlation.get("attempt_id", ""),
                "worker_id": resolved_worker_id,
            })
            _save_session_anchor(session)
            workspace = session.setdefault("workspace_context", {})
            workspace.update({
                "scope_mode": "single" if _is_local_worker_id(resolved_worker_id) else "cluster",
                "worker_id": resolved_worker_id,
                "device_ids": list(req.devices),
                "suite_path": req.test_suite,
                "cluster_job_id": correlation["cluster_job_id"],
                "attempt_id": correlation.get("attempt_id", ""),
                "origin_page": "agent",
            })
    else:
        _append_step(session, "启动测试失败", "error", payload.get("error") or payload.get("message", ""), payload)
    return payload


async def _run_pre_actions(
    session: dict[str, Any],
    plan: dict[str, Any],
    request: AgentRequestShim,
) -> dict[str, Any]:
    actions = plan.get("pre_actions") or []
    if not actions:
        return {"success": True}

    req = plan.get("request") or {}
    devices = req.get("devices") or []
    worker_id = req.get("worker_id") or _local_worker_id()
    for action in actions:
        if action.get("type") != "connect_wifi":
            continue
        if not _is_local_worker_id(worker_id):
            from features.cluster import ClusterDeviceAction, device_action

            response = await device_action(ClusterDeviceAction(
                worker_id=worker_id,
                devices=devices,
                action="wifi",
                ssid=action.get("ssid") or "",
                password=action.get("password") or "",
            ), request)
            payload = _jsonable(response)
            wifi_ssid = action.get("ssid") or ""
        else:
            from features.devices import WifiConnectRequest, connect_wifi

            wifi_req = WifiConnectRequest(
                devices=devices,
                ssid=action.get("ssid"),
                password=action.get("password"),
            )
            response = await connect_wifi(wifi_req, request)
            payload = _json_body(response)
            wifi_ssid = wifi_req.ssid
        summary = payload.get("summary") or {}
        results = payload.get("results") or []
        if not summary and results:
            summary = {
                "success": sum(1 for item in results if item.get("success")),
                "total": len(results),
            }
        detail = f"{wifi_ssid}: 成功 {summary.get('success', 0)}/{summary.get('total', len(devices))}"
        if payload.get("success"):
            _append_step(session, "连接 WiFi", "done", detail, payload)
        else:
            _append_step(session, "连接 WiFi 失败", "error", payload.get("error") or payload.get("message", ""), payload)
            return payload
    return {"success": True}


async def _monitor_agent_run(
    session_id: str, request_shim: AgentRequestShim, monitor_token: str | None = None
) -> None:
    session = _agent_sessions.get(session_id)
    if not session:
        return

    plan = session.get("pending_plan") or session.get("active_run", {}).get("plan")
    # 恢复会话（Controller 重启后从锚点重建）没有持久化 plan（防凭据落盘），
    # 降级为纯跟踪：等待任务终态、关联报告、更新工作区；retry/分析因缺少
    # 计划与凭据自动跳过（policy 视为空）。非恢复会话无 plan 则无需监控。
    if not plan and not session.get("recovered"):
        return
    policy = (plan or {}).get("policy", {})
    client_id = session.get("owner_id") or session.get("client_id")
    # 报告按请求主体的 resource_owner_id 分区（ADR 0010），与会话里用于
    # 运行时状态的 client_id 分开解析；agent 主体由此映射回注册账号。
    # 请求对象缺身份（恢复重建的监控、极简 shim）时回退锚点里的 owner_id。
    report_owner_id = owner_id_from_request_shim(request_shim) or str(session.get("owner_id") or "")
    retries_left = int(policy.get("retry_count") or 0)
    attempt = 0
    latest_report_timestamp = None
    baseline_report_timestamp = (session.get("active_run") or {}).get("baseline_report_timestamp")

    try:
        while True:
            if not _monitor_run_is_current(session, monitor_token):
                # 本轮监控已被替换：立即退出，不改任何共享状态。
                return
            active_run = session.get("active_run") or {}
            cluster_job_id = active_run.get("cluster_job_id") or ""
            if cluster_job_id:
                from features.cluster import get_cluster_service

                repository = get_cluster_service().repository
                job = repository.get_job(cluster_job_id)
                while job and job.get("status") in _ACTIVE_CLUSTER_JOB_STATUSES:
                    await asyncio.sleep(3)
                    job = repository.get_job(cluster_job_id)
                if not _monitor_run_is_current(session, monitor_token):
                    # 长等待期间被新一轮监控替换：不再读结果/写步骤。
                    return
                if not job:
                    if _monitor_run_is_current(session, monitor_token):
                        _finish_run_state(session, "error")
                        _append_step(session, "读取集群任务", "error", f"任务不存在: {cluster_job_id}")
                    return

                attempt_id = active_run.get("attempt_id") or job.get("current_attempt_id", "")
                report = None
                for _ in range(20):
                    report = _report_for_cluster_job(
                        cluster_job_id,
                        attempt_id,
                        str(report_owner_id or ""),
                    )
                    if report and report.get("status") != "collecting":
                        break
                    if job.get("status") != "completed" and not report:
                        break
                    await asyncio.sleep(1)
                if not _monitor_run_is_current(session, monitor_token):
                    return
                latest_report_timestamp = report.get("timestamp") if report else None
                job_failed = job.get("status") != "completed"
                failed = job_failed or _report_failed(report)
                _append_step(
                    session,
                    "读取集群测试结果",
                    "done" if not job_failed else "error",
                    f"{cluster_job_id} / {job.get('status')}"
                    + (f" / 报告 {latest_report_timestamp}" if latest_report_timestamp else " / 未生成报告"),
                    {"job": job, "report": report or {}},
                )
                if not _monitor_run_is_current(session, monitor_token):
                    # 已被新一轮监控替换：跳过 retry/分析/终态写入。
                    return

                workspace = session.setdefault("workspace_context", {})
                assigned_worker_id = job.get("assigned_worker_id", "")
                workspace.update({
                    "scope_mode": "single" if _is_local_worker_id(assigned_worker_id) else "cluster",
                    "worker_id": assigned_worker_id,
                    "cluster_job_id": cluster_job_id,
                    "attempt_id": attempt_id,
                    "report_timestamp": latest_report_timestamp or "",
                    "report_id": (report or {}).get("report_id", ""),
                    "artifact_id": (report or {}).get("artifact_id", ""),
                    "origin_page": "agent",
                })
                _save_session_anchor(session)

                if failed and retries_left > 0 and latest_report_timestamp:
                    attempt += 1
                    retries_left -= 1
                    _append_message(
                        session, "assistant",
                        f"检测到失败，开始第 {attempt} 次 retry：{latest_report_timestamp}",
                    )
                    payload = await _start_test_with_plan(
                        session, request_shim, plan, latest_report_timestamp
                    )
                    if not payload.get("success"):
                        if _monitor_run_is_current(session, monitor_token):
                            _finish_run_state(session, "error")
                            _append_message(
                                session, "assistant",
                                f"retry 启动失败：{payload.get('error') or payload.get('message')}",
                            )
                        return
                    continue

                if failed and policy.get("analyze_on_failure") and report:
                    session["status"] = "analyzing"
                    await _run_failure_analysis_pipeline(
                        session, plan, report, request_shim
                    )
                elif failed:
                    _append_message(
                        session, "assistant",
                        f"集群测试失败。任务：{cluster_job_id}，报告：{latest_report_timestamp or '未生成'}",
                    )
                else:
                    _append_message(
                        session, "assistant",
                        f"集群测试完成。任务：{cluster_job_id}，报告：{latest_report_timestamp or '未生成'}",
                        data={
                            "cluster_job_id": cluster_job_id,
                            "attempt_id": attempt_id,
                            "report_timestamp": latest_report_timestamp or "",
                        },
                    )
                if _monitor_run_is_current(session, monitor_token):
                    _finish_run_state(session, "done")
                return

            user_state = get_or_create_user_state(client_id)
            while user_state.get("running", False):
                await asyncio.sleep(3)
                user_state = get_or_create_user_state(client_id)

            await asyncio.sleep(1)
            report = _latest_report_for_client(
                report_owner_id, exclude_timestamp=baseline_report_timestamp
            )
            if not _monitor_run_is_current(session, monitor_token):
                # 等待期间被新一轮计划替换：跳过步骤/消息写入，终态与锚点清理由新任务负责。
                return
            if report:
                latest_report_timestamp = report.get("timestamp")
                failed = _report_failed(report)
                _append_step(
                    session,
                    "读取测试报告",
                    "done",
                    f"{latest_report_timestamp}，失败 {report.get('fail', 0)} / 总计 {report.get('total', 0)}",
                    {"report": report},
                )
            else:
                failed = False
                _append_step(session, "读取测试报告", "warning", "没有找到本次测试报告")

            if failed and retries_left > 0 and latest_report_timestamp:
                attempt += 1
                retries_left -= 1
                _append_message(session, "assistant", f"检测到失败，开始第 {attempt} 次 retry：{latest_report_timestamp}")
                payload = await _start_test_with_plan(session, request_shim, plan, latest_report_timestamp)
                if not payload.get("success"):
                    if _monitor_run_is_current(session, monitor_token):
                        _finish_run_state(session, "error")
                        _append_message(session, "assistant", f"retry 启动失败：{payload.get('error') or payload.get('message')}")
                    return
                baseline_report_timestamp = latest_report_timestamp
                continue

            if not _monitor_run_is_current(session, monitor_token):
                # 已被新一轮计划替换：不再改共享状态，终态与锚点清理由新任务负责。
                return

            if failed and policy.get("analyze_on_failure") and latest_report_timestamp and report:
                session["status"] = "analyzing"
                await _run_failure_analysis_pipeline(
                    session, plan, report, request_shim
                )
            elif failed:
                _append_message(session, "assistant", f"测试完成但有失败。最近报告：{latest_report_timestamp or '未找到'}")
            else:
                _append_message(session, "assistant", f"测试完成，未检测到失败报告。最近报告：{latest_report_timestamp or '未找到'}")

            if _monitor_run_is_current(session, monitor_token):
                _finish_run_state(session, "done")
            return
    except asyncio.CancelledError:
        if _monitor_run_is_current(session, monitor_token):
            _finish_run_state(session, "cancelled")
            _append_step(session, "Agent 监控已停止", "warning", "用户取消了 Agent 后台监控")
        raise
    except Exception:
        if _monitor_run_is_current(session, monitor_token):
            _finish_run_state(session, "error")
            err = record_internal_error(logger, "Agent 监控", "[Agent] monitor failed")
            _append_step(session, "Agent 监控异常", "error", err)
            _append_message(session, "assistant", err)
        else:
            logger.debug("旧 Agent 监控任务被新一轮监控替换，跳过终态清理", exc_info=True)
    finally:
        # 只有任务表里登记的仍是自己时才移除，避免旧任务退出时误删新任务。
        if _agent_monitor_tasks.get(session_id) is asyncio.current_task():
            _agent_monitor_tasks.pop(session_id, None)


async def _execute_plan(session: dict[str, Any], request: Request, plan: dict[str, Any]) -> dict[str, Any]:
    req = plan.get("request", {})
    if not req.get("devices"):
        return {"success": False, "error": "没有可用设备"}
    if not req.get("test_suite") and not req.get("retry_dir"):
        return {"success": False, "error": "没有匹配到测试套件"}

    # 报告基线同样按请求主体的 resource_owner_id 解析（ADR 0010）。
    latest_existing_report = _latest_report_for_client(owner_id_from_request(request))
    # 每轮监控的唯一 token：旧任务的取消回调据此识别自己已过期，不再
    # 覆盖新一轮的 active_run / 恢复锚点。
    monitor_token = str(uuid.uuid4())
    if not plan.get("operation_id"):
        plan["operation_id"] = str(uuid.uuid4())
    session["status"] = "running"
    session["active_run"] = {
        "plan": plan,
        # 计划的唯一操作 ID：重复确认/重试链路的可追溯键；旧会话中的
        # 计划可能没有该字段，这里补齐兜底。
        "operation_id": plan["operation_id"],
        "started_at": _now_iso(),
        "baseline_report_timestamp": latest_existing_report.get("timestamp") if latest_existing_report else None,
        "worker_id": req.get("worker_id") or _local_worker_id(),
        "monitor_token": monitor_token,
    }
    _save_session_anchor(session)
    request_shim = AgentRequestShim(request)

    payload = await execute_plan_start(
        session, request_shim, plan, monitor_token,
        pre_actions=_run_pre_actions, start_test=_start_test_with_plan,
    )
    if not payload.get("success"):
        return payload

    session["status"] = "monitoring"
    _append_message(session, "assistant", "测试已启动，我会继续监控完成状态，并按计划处理 retry/分析。")
    old_task = _agent_monitor_tasks.pop(session["session_id"], None)
    if old_task and not old_task.done():
        old_task.cancel()
    task = asyncio.create_task(
        _monitor_agent_run(session["session_id"], request_shim, monitor_token)
    )
    _agent_monitor_tasks[session["session_id"]] = task
    session["monitor_task_id"] = id(task)
    return payload
