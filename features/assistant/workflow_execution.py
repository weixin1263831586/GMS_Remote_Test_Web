"""WorkflowDraft 执行与计划操作（从 api.py 拆出，file-size baseline）。

职责：把已确认的 WorkflowDraft 提交到 GMS ATS（唯一执行通道），跟踪
run 终态，并处理待确认草案的 plan.update/replan/cancel/status 操作。
本模块不实现任何编排状态机；资源预检只读。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import uuid
from typing import Any

from features.assistant.plan_context import (
    _list_suites,
    is_local_worker_id,
    local_worker_id,
)
from features.assistant.session_log import (
    _append_message,
    _append_step,
    complete_recovery_step,
)
from features.assistant.session_registry import (
    AgentRequestShim,
    _agent_monitor_tasks,
    _agent_sessions,
    _now_iso,
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
from features.assistant.test_runs import MAX_AGENT_RETRIES
from features.assistant.workflow_draft import (
    detect_plan_action,
    draft_blocks,
    preflight_draft,
    summarize_draft,
    update_draft,
)
from foundation.error_model import record_internal_error


logger = logging.getLogger(__name__)

WORKFLOW_PLAN_TYPE = "workflow_draft"

# 步骤面板反查标题：update_workflow_run_step 与 execute_workflow_plan 必须共用。
WORKFLOW_CREATE_STEP_TITLE = "创建 ATS 工作流"

_WORKFLOW_CONFIRM_RE = re.compile(r"^(确认执行|开始执行|执行|确认|start|run|go)$", re.IGNORECASE)

def is_workflow_confirm_message(message: str) -> bool:
    return bool(_WORKFLOW_CONFIRM_RE.match((message or "").strip()))


# automation/ui/state.js STAGE_LABELS_ZH 的后端镜像：步骤面板的实时进展
# 文案需要中文阶段名，与 GMS ATS 页显示保持一致。
RUN_STAGE_LABELS_ZH = {
    "queued": "排队", "jenkins_queued": "构建排队", "jenkins_building": "编译中",
    "artifact_fetching": "固件下载中",
    "artifact_ready": "固件就绪", "waiting_device": "等待设备", "device_locked": "设备已锁",
    "flashing": "刷机中", "flash_completed": "刷机完成", "aoa_recovering": "恢复 USB 调试",
    "post_flash_verifying": "固件校验中", "flash_verified": "固件已校验", "testing": "启动测试",
    "test_running": "测试中", "report_collecting": "收集报告", "analyzing": "分析中",
    "reporting": "上报中", "completed": "完成",
    "failed": "失败", "cancelled": "已取消", "jenkins_failed": "编译失败",
    "artifact_missing": "固件缺失", "flash_failed": "刷机失败", "aoa_failed": "AOA 恢复失败",
    "test_failed": "测试失败", "analysis_failed": "分析失败", "reporting_failed": "上报失败",
}


def workflow_run_progress(run: dict[str, Any]) -> dict[str, Any]:
    from features.automation import artifact_download_progress

    status = str(run.get("status") or "")
    return {
        "status": status,
        "current_stage": run.get("current_stage") or status,
        "error": str(run.get("error") or ""),
        "updated_at": run.get("updated_at") or _now_iso(),
        "artifact_download": artifact_download_progress(run),
    }


def update_workflow_run_step(
    session: dict[str, Any], run_id: str, run: dict[str, Any]
) -> bool:
    """就地更新「创建 ATS 工作流」步骤的实时进展，供步骤面板轮询展示。

    返回 status 是否发生变化：监控循环只在变化时持久化恢复锚点，
    避免每 5 秒一次落盘放大写放大。
    """
    step = next(
        (item for item in reversed(session.get("steps") or [])
         if str((item.get("data") or {}).get("automation_run_id") or "") == run_id
         and item.get("title") == WORKFLOW_CREATE_STEP_TITLE),
        None,
    )
    if not step:
        # 恢复会话的 steps 不随锚点持久化：补一个可被本函数就地更新的
        # 进展步骤；run_id 与当前 active_run 不符时不补（旧监控轮次）。
        if str((session.get("active_run") or {}).get("automation_run_id") or "") != run_id:
            return False
        _append_step(
            session, WORKFLOW_CREATE_STEP_TITLE, "running",
            f"恢复监控 Run {run_id}",
            {"automation_run_id": run_id},
        )
        step = session["steps"][-1]
    status = str(run.get("status") or "")
    label = RUN_STAGE_LABELS_ZH.get(status) or status or "未知"
    if status == "completed":
        step["status"] = "done"
    elif status == "cancelled":
        step["status"] = "warning"
    elif status:
        from features.automation import AUTOMATION_TERMINAL_STATUSES

        step["status"] = (
            "error" if status in AUTOMATION_TERMINAL_STATUSES else "running"
        )
    else:
        step["status"] = "running"
    step["detail"] = f"Run {run_id} · 当前阶段：{label}（{status or 'unknown'}）"
    progress = workflow_run_progress(run)
    if progress["error"]:
        step["detail"] += f"；原因：{progress['error']}"
    previous = (step.get("data") or {}).get("progress") or {}
    changed = any(previous.get(key) != progress[key] for key in ("status", "current_stage", "error"))
    step.setdefault("data", {})["status"] = status
    step.setdefault("data", {})["progress"] = progress
    from features.assistant.session_log import _settle_step

    _settle_step(step)  # 终态补记 finished_at / duration_seconds
    session["updated_at"] = _now_iso()
    return changed


def wants_message_execution(req, message: str) -> bool:
    return bool(req.execute) or is_workflow_confirm_message(message)


def workflow_preflight_lookups(
    workspace: dict[str, Any], owner_id: str
) -> dict[str, Any]:
    """只读资源查询回调：固件分享 / 全局设备定位 / AOA / 套件。"""

    def share_lookup(share_id: str) -> dict[str, Any]:
        from features.firmware import firmware_share_record_snapshot

        snapshot = firmware_share_record_snapshot(share_id)
        if not snapshot:
            return {"ok": False, "reason": f"固件分享不存在: {share_id}"}
        if snapshot.get("expired"):
            return {"ok": False, "reason": f"固件分享已过期: {share_id}"}
        return {
            # 预检只验证分享记录有效（存在且未过期）；真实 SFTP 认证、
            # 路径与文件读取发生在执行期暂存阶段，文案不得给过强承诺。
            "ok": True,
            "detail": (
                f"{snapshot.get('filename') or snapshot.get('path')}（{snapshot.get('size', 0)} 字节，"
                "分享记录有效；文件可读取性在烧写暂存时校验）"
            ),
        }

    def device_lookup(serial: str, worker_id: str | None) -> dict[str, Any]:
        from features.cluster import get_cluster_service

        try:
            rows = get_cluster_service().repository.list_devices()
        except Exception as exc:
            return {"ok": False, "reason": f"集群设备库存不可用: {exc}"}
        serial_rows = [
            row for row in rows if str(row.get("serial") or "") == serial
        ]
        if not serial_rows:
            return {"ok": False, "reason": f"设备 {serial} 未出现在任何 Worker 的设备清单中"}
        matches = [
            row for row in serial_rows
            if not worker_id or str(row.get("worker_id") or "") == worker_id
        ]
        # 序列号跨 Worker 重复时结果依赖库存顺序——必须消歧：serial 与
        # Worker 的 USB 物理绑定是唯一身份；未指定 Worker 且跨机重复时
        # 拒绝预检，要求显式指定 Worker。
        workers = sorted({str(row.get("worker_id") or "") for row in serial_rows})
        if len(workers) > 1 and not worker_id:
            return {
                "ok": False,
                "reason": (
                    f"设备 {serial} 出现在多个 Worker（{', '.join(workers)}）上，"
                    "无法唯一定位；请指定 Worker（如「worker:<ID>」）后再规划"
                ),
            }
        if not matches:
            found_on = "、".join(workers)
            return {
                "ok": False,
                "reason": (
                    f"设备 {serial} 在 {found_on or '未知'} 上，"
                    f"与指定 Worker {worker_id} 不符，不会自动换设备"
                ),
            }
        row = matches[0]
        resolved_worker = str(row.get("worker_id") or "")
        # 与 automation.create_run 的执行期校验同标准：设备必须出现在
        # 指定 Worker 的清单中，且 state == available（service.py 会以
        # "Devices are unavailable" 拒绝）；预检放行执行期必挂的组合。
        claimed = bool(row.get("claimed"))
        state = str(row.get("state") or "")
        detail = f"Worker {resolved_worker} / {state or 'unknown'}"
        if claimed:
            return {"ok": False, "reason": f"设备 {serial} 被占用（{row.get('claim_username') or row.get('claim_owner_id') or '其他任务'}），不会自动换设备"}
        if state != "available":
            return {"ok": False, "reason": f"设备 {serial} 当前状态 {state or 'unknown'}，非空闲（需 available），不会自动换设备"}
        return {"ok": True, "worker_id": resolved_worker, "detail": detail}

    def aoa_lookup(profile: str, _owner: str) -> dict[str, Any]:
        from features.devices import profile_store

        try:
            profiles = profile_store().list_profiles(owner_id, include_all=False)
        except Exception as exc:
            return {"ok": False, "reason": f"AOA 配置读取失败: {exc}"}
        if any(p.get("name") == profile or p.get("id") == profile for p in profiles):
            return {"ok": True, "detail": f"AOA 配置 {profile} 存在于当前账号"}
        return {"ok": False, "reason": f"AOA 配置 {profile} 不存在或不属于当前账号"}

    def suite_lookup(test_type: str, worker_id: str | None) -> dict[str, Any]:
        context = dict(workspace or {})
        if worker_id:
            context["worker_id"] = worker_id
            context["scope_mode"] = "cluster" if not is_local_worker_id(worker_id) else "single"
        try:
            suites = _list_suites(context)
        except Exception as exc:  # SSH/网络/超时等基础设施异常
            # 套件查询涉及远程访问（审核 P1）：失败只把 test 阶段标记为
            # 阻塞，不得让异常中断整个对话预检（权限等其余检查仍正常展示）。
            logger.warning("[Agent] suite lookup failed during preflight: %s", exc)
            return {"ok": False, "reason": f"测试套件查询暂不可用（远程访问失败）：{exc}"}
        matched = [
            suite for suite in suites
            if str(suite.get("test_type") or suite.get("suite_type") or "").upper() == test_type
        ]
        if matched:
            suite = matched[0]
            # 返回 tools_path：preflight_draft 把它冻结进草案 test.test_suite，
            # 提交 ATS 后执行同一版本；变化时由 create_run 的套件校验拒绝。
            return {
                "ok": True,
                "detail": suite.get("tools_path") or suite.get("full_path") or test_type,
                "tools_path": suite.get("tools_path") or suite.get("full_path") or "",
            }
        return {"ok": False, "reason": f"该 Worker 上没有 {test_type} 的可用测试套件"}

    return {
        "share_lookup": share_lookup,
        "device_lookup": device_lookup,
        "aoa_lookup": aoa_lookup,
        "suite_lookup": suite_lookup,
    }


def refresh_workflow_preflight(
    session: dict[str, Any],
    draft: dict[str, Any],
    *,
    request: Any = None,
) -> dict[str, Any]:
    workspace = session.get("workspace_context") or {}
    owner_id = str(session.get("owner_id") or "")
    # 权限门槛必须先于任何远程资源访问（审核 P1）：human-only / 能力缺失
    # 直接短路返回，未授权请求不得发起 SSH 套件查询、Worker 探测或固件
    # 下载——否则网络不可用时预检以异常收场，用户看不到权限提示。
    permission = workflow_permission_block(draft, workspace, request)
    if permission is not None:
        return {"ready": False, "checks": [permission], "blocks": [permission]}
    preflight = preflight_draft(draft, **workflow_preflight_lookups(workspace, owner_id))
    # 权限门槛通过后才做集群执行预检：复用 automation preflight（与
    # create_run 同一入口校验），覆盖 Worker 在线、命令 Agent、磁盘/
    # 内存准入、高级执行参数（preparations/sharding）Worker 能力与
    # retry 策略——聊天预检不允许比最终创建更宽松（ADR 0012）。
    execution = workflow_execution_readiness_block(draft, workspace, request)
    if execution is not None:
        preflight["checks"] = [*list(preflight.get("checks") or []), execution]
        preflight["blocks"] = [*list(preflight.get("blocks") or []), execution]
        preflight["ready"] = False
    return preflight


# 能力 → 中文说明：聊天预检与创建失败提示共用同一张表，避免两处文案漂移。
_WORKFLOW_CAPABILITY_LABELS: dict[str, str] = {
    "firmware.stage": "烧写固件（firmware.stage）",
    "devices.lease": "设备预留（devices.lease）",
    "devices.read": "读取设备（devices.read）",
    "devices.use_leased": "操作预留设备（devices.use_leased）",
    "tests.execute": "启动测试（tests.execute）",
    "tests.cancel": "取消测试（tests.cancel）",
    "build.read": "读取构建配置（build.read）",
    "build.execute": "启动构建（build.execute）",
    "build.cancel": "取消构建（build.cancel）",
}


def workflow_execution_readiness_block(
    draft: dict[str, Any], workspace: dict[str, Any], request: Any = None
) -> dict[str, Any] | None:
    """把 automation preflight（create 的权威校验）作为聊天预检的一部分。

    资源级 lookups 只回答"分享/设备/AOA/套件是否存在"，这里让最终
    ``create_run`` 会在集群侧做的全部准入校验原样跑一遍；任何 ValueError
    （含 MachineAuthorityError）转成阻塞项而不是异常，保证预检摘要完整。
    """
    from features.auth import get_authenticated_user, is_elevated
    from features.automation import get_automation_service

    principal = get_authenticated_user(request) if request is not None else None
    elevated = bool(is_elevated(request)) if request is not None else False
    try:
        get_automation_service().preflight(
            run_body_from_draft(draft, workspace),
            principal=principal,
            elevated=elevated,
        )
    except ValueError as exc:
        return {"stage": "execution", "ok": False, "reason": str(exc)}
    except Exception as exc:  # 基础设施异常：按依赖不可用阻塞，而非崩溃
        return {
            "stage": "execution",
            "ok": False,
            "reason": f"执行预检暂不可用（依赖服务异常）：{exc}",
        }
    return None


def workflow_permission_block(
    draft: dict[str, Any], workspace: dict[str, Any], request: Any = None
) -> dict[str, Any] | None:
    """创建者权限预检：与 automation create_run 同一能力编译器。

    返回阻塞项 dict（计入 preflight.ready），无阻塞或无法判定时返回
    ``None``。匿名/dev 模式调用方与 create_run 的语义一致（空 union 直接
    放行）；提权会话（管理员重认证）视为同意全部计划能力，与固件工作台
    的门槛对齐（ADR 0012）。
    """
    if request is None:
        return None
    from features.auth import (
        AGENT_ROLE,
        automation_required_capabilities,
        get_authenticated_user,
        is_elevated,
        is_machine_principal,
    )

    principal = get_authenticated_user(request)
    if principal is None:
        return None
    # human-only 是创建边界：机器主体在预检期就显示阻塞，而不是预检
    # Ready 后由 create_run 拒绝（ADR 0012）。
    if is_machine_principal(principal) or principal.role in {AGENT_ROLE, "worker_service"}:
        return {
            "stage": "permission",
            "ok": False,
            "reason": "ATS 工作流只能由人类账号创建（human-only）；"
                      "当前会话是机器身份（Agent/Worker），预检不通过。",
        }
    if is_elevated(request):
        return None
    test_plan = run_body_from_draft(draft, workspace).get("test_plan")
    missing = [
        name for name in automation_required_capabilities(test_plan)
        if not principal.has_permission(name)
    ]
    if not missing:
        return None
    labels = "、".join(_WORKFLOW_CAPABILITY_LABELS.get(name, name) for name in missing)
    return {
        "stage": "permission",
        "ok": False,
        "reason": (
            f"当前账号缺少工作流所需权限：{labels}。"
            "完成管理员提权（重新验证管理员凭据）后重试，或联系管理员为账号授予相应能力。"
        ),
    }


def run_body_from_draft(draft: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    """把草案转换为 automation create_run 的请求体（唯一执行通道）。"""
    device = draft.get("device") or {}
    firmware = draft.get("firmware") or {}
    aoa = draft.get("aoa") or {}
    test = draft.get("test") or {}
    worker_id = str(device.get("worker_id") or workspace.get("worker_id") or "")
    flash_only = (str(test.get("scope") or "") == "skip")
    test_plan: dict[str, Any] = {
        "flash": {"mode": "firmware", "firmware_share_id": firmware.get("share_id") or ""},
    }
    if flash_only:
        # 纯烧写/纯 AOA：显式 test_mode=skip（orchestrator 在固件校验通过后
        # 直接收尾），不带空 test_type，否则会被契约当作漏填拒绝。
        test_plan["test_mode"] = "skip"
    else:
        test_plan["test_type"] = test.get("type") or ""
    if worker_id:
        test_plan["worker_id"] = worker_id
    if aoa.get("mode") == "user_build_recovery":
        test_plan["aoa"] = {
            "mode": "user_build_recovery",
            "profile": aoa.get("profile") or "",
        }
    if test.get("module"):
        test_plan["test_module"] = test["module"]
    if test.get("case"):
        test_plan["test_case"] = test["case"]
    # 预检冻结的套件精确绑定到 run：执行期不再按 test_type 重新模糊选择，
    # 避免预检看到的套件与实际执行的版本不一致（automation 会校验该
    # tools_path 在所选 Worker 上仍可用，变化时显式失败而非静默换版本）。
    if test.get("test_suite"):
        test_plan["test_suite"] = str(test["test_suite"])
    # 连接 WiFi 落入执行期 preparations（worker_agent 测试准备模板），
    # 仅在测试阶段实际运行时生效。
    if test.get("connect_wifi") and (test.get("scope") or "full") != "skip":
        test_plan["preparations"] = ["wifi_on"]
    retry_count = int(test.get("retry_count") or 0)
    if retry_count > 0:
        test_plan["retry_policy"] = {"max_retries": min(retry_count, MAX_AGENT_RETRIES)}
    return {
        "source_type": "assistant_chat",
        "source_key": str(draft.get("operation_id") or ""),
        "devices": [device.get("serial") or ""],
        "test_plan": test_plan,
    }


async def execute_workflow_plan(
    session: dict[str, Any], request, draft: dict[str, Any]
) -> dict[str, Any]:
    """提交 WorkflowDraft 到 automation 服务并启动状态跟踪。"""
    from features.auth import get_authenticated_user
    from features.automation import get_automation_service

    automation_service = get_automation_service()
    blocks = draft_blocks(draft)
    if blocks:
        return {"success": False, "error": blocks[0].get("reason") or "工作流缺少必要实体"}
    try:
        body = run_body_from_draft(draft, session.get("workspace_context") or {})
        from features.auth import MachineAuthorityError, is_elevated

        principal = get_authenticated_user(request)
        run = automation_service.create_run(
            body,
            created_by=str(session.get("owner_id") or ""),
            principal=principal,
            elevated=is_elevated(request),
        )
    except MachineAuthorityError as exc:
        # 与聊天预检同一张能力说明表：报错即给下一步（提权或找管理员），
        # 不透出英文原始能力清单。
        from features.auth import automation_required_capabilities

        missing = [
            name for name in automation_required_capabilities(body.get("test_plan"))
            if principal is None or not principal.has_permission(name)
        ]
        if missing:
            labels = "、".join(
                _WORKFLOW_CAPABILITY_LABELS.get(name, name) for name in missing
            )
            logger.warning("[Agent] workflow run rejected for missing capabilities: %s", exc)
            return {
                "success": False,
                "error": (
                    f"当前账号缺少工作流所需权限：{labels}。"
                    "完成管理员提权（重新验证管理员凭据）后重试，或联系管理员为账号授予相应能力。"
                ),
            }
        return {"success": False, "error": str(exc)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except sqlite3.IntegrityError:
        # source_key 唯一索引命中：同一草案被重复确认（双击/并发），
        # 给语义化提示而不是内部错误文案。
        return {
            "success": False,
            "error": "该计划已提交过 ATS 工作流，请勿重复确认；可在 GMS ATS 页查看已有 Run",
        }
    except Exception as exc:
        message = record_internal_error(logger, "工作流创建", "[Agent] workflow run create failed")
        logger.error("%s: %s", message, exc)
        return {"success": False, "error": message}

    monitor_token = str(uuid.uuid4())
    session["status"] = "running"
    # 审核修正：约束作用域绑定单个未提交计划。计划已成功提交执行，草案
    # 自含 user_constraints 快照，会话级冻结约束在此关闭作用域——下一个
    # 独立任务从干净状态开始，不被上一轮已执行完的要求（如 CTS/AOA）
    # 污染。提交失败时不清理：同一计划仍需按原约束重试。
    session["workflow_user_constraints"] = {}
    session["active_run"] = {
        "workflow_draft": draft,
        "automation_run_id": run.get("id") or "",
        "operation_id": draft.get("operation_id") or "",
        "started_at": _now_iso(),
        "monitor_token": monitor_token,
        "worker_id": (draft.get("device") or {}).get("worker_id") or local_worker_id(),
    }
    draft["status"] = "submitted"
    _save_session_anchor(session)
    _append_step(
        session, WORKFLOW_CREATE_STEP_TITLE, "running",
        f"Run {run.get('id')}（烧写 → AOA → 测试，状态机推进由 GMS ATS 执行）",
        {"automation_run_id": run.get("id"), "status": run.get("status"),
         "progress": workflow_run_progress(run)},
    )
    request_shim = AgentRequestShim(request)
    old_task = _agent_monitor_tasks.pop(session["session_id"], None)
    if old_task and not old_task.done():
        old_task.cancel()
    task = asyncio.create_task(
        monitor_workflow_run(session["session_id"], request_shim, monitor_token)
    )
    _agent_monitor_tasks[session["session_id"]] = task
    session["monitor_task_id"] = id(task)
    return {"success": True, "automation_run_id": run.get("id")}


async def monitor_workflow_run(
    session_id: str, request_shim, monitor_token: str
) -> None:
    """跟踪 ATS run 直到终态；不重复实现编排逻辑。"""
    from features.automation import (
        AUTOMATION_TERMINAL_STATUSES,
        AutomationNotFoundError,
        get_automation_service,
    )

    automation_service = get_automation_service()
    session = _agent_sessions.get(session_id)
    if not session:
        return
    run_id = str((session.get("active_run") or {}).get("automation_run_id") or "")
    if not run_id:
        return
    workspace_linked = False
    status = ""
    run: dict[str, Any] | None = None
    active = session.setdefault("active_run", {})
    try:
        while True:
            if not _monitor_run_is_current(session, monitor_token):
                return
            try:
                run = automation_service.get_run(run_id)
            except AutomationNotFoundError:
                run = None
            if not run:
                if _monitor_run_is_current(session, monitor_token):
                    complete_recovery_step(
                        session, active, status="warning", detail="恢复后读取 ATS 工作流失败。",
                    )
                    _finish_run_state(session, "error")
                    _append_step(session, "读取 ATS 工作流", "error", f"运行不存在: {run_id}")
                return
            if not workspace_linked and run.get("cluster_job_id"):
                workspace_linked = True
                workspace = session.setdefault("workspace_context", {})
                workspace.update({
                    "automation_run_id": run_id,
                    "cluster_job_id": run["cluster_job_id"],
                    "origin_page": "agent",
                })
                _save_session_anchor(session)
            status = str(run.get("status") or "")
            if update_workflow_run_step(session, run_id, run):
                _save_session_anchor(session)
            if status in AUTOMATION_TERMINAL_STATUSES:
                break
            # 非终态：后台任务真实状态已确认，收敛重启恢复步骤。
            complete_recovery_step(session, active)
            await asyncio.sleep(5)
        if not _monitor_run_is_current(session, monitor_token):
            return
        # cancelled 不是失败：用户/系统主动取消，收尾为 warning，文案不引导 retry。
        cancelled = status == "cancelled"
        failed = status not in ("completed", "cancelled")
        _append_step(
            session, "ATS 工作流结束", "warning" if cancelled else ("error" if failed else "done"),
            f"Run {run_id} → {status}"
            + (f"，报告 {run.get('report_timestamp')}" if run.get("report_timestamp") else "")
            + (f"；原因：{run['error']}" if run.get("error") else ""),
            {"automation_run_id": run_id, "status": status,
             "progress": workflow_run_progress(run),
             "report_timestamp": run.get("report_timestamp") or ""},
        )
        if run.get("report_timestamp"):
            workspace = session.setdefault("workspace_context", {})
            workspace.update({
                "report_timestamp": run["report_timestamp"],
                "report_id": run.get("report_id") or "",
                "automation_run_id": run_id,
            })
            _save_session_anchor(session)
        _finish_run_state(session, "warning" if cancelled else ("error" if failed else "done"))
        closing_hint = (
            "可在 GMS ATS 页查看时间线。" if not failed and not cancelled
            else "Run 已取消，可在 GMS ATS 页确认设备与任务状态。"
            if cancelled
            else "可在 GMS ATS 页查看失败原因或发起 retry。"
        )
        _append_message(
            session, "assistant",
            f"工作流 {run_id} 已结束：{status}。"
            + (f"原因：{run['error']}。" if run.get("error") else "")
            + closing_hint,
            data={"automation_run_id": run_id, "status": status},
        )
    except asyncio.CancelledError:
        if _monitor_run_is_current(session, monitor_token):
            _finish_run_state(session, "cancelled")
            _append_step(session, "Agent 监控已停止", "warning", "用户取消了工作流监控；Run 本身未取消，可在 GMS ATS 页停止")
        raise
    except Exception:
        if _monitor_run_is_current(session, monitor_token):
            _finish_run_state(session, "error")
            err = record_internal_error(logger, "工作流监控", "[Agent] workflow monitor failed")
            _append_step(session, "工作流监控异常", "error", err)
    finally:
        if _agent_monitor_tasks.get(session_id) is asyncio.current_task():
            _agent_monitor_tasks.pop(session_id, None)


async def handle_workflow_plan_message(
    session: dict[str, Any], message: str, pending_plan: dict[str, Any],
    request: Any = None,
) -> dict[str, Any] | None:
    """处理待确认工作流草案的修改/重规划/取消/状态查询。

    返回 None 表示消息与草案无关，交回通用意图路由。``request`` 用于
    replan 刷新预检时重算创建者权限阻塞（与计划生成时同一编译器）。
    """
    draft = pending_plan.setdefault("draft", {})
    preflight = pending_plan.get("preflight") or {"ready": False, "checks": []}
    action = detect_plan_action(message)
    # 已提交（或提交中）的计划是执行历史，不是活草案：replan/status 只读
    # 展示；修改意见不套用（v2 无法再执行，会产生“看起来可执行”的假象）。
    # 新目标必须重新描述指令，由 _make_workflow 生成全新草案与新 operation_id。
    if str(pending_plan.get("submit_state") or "") in {"submitting", "submitted"}:
        if action == "status":
            _append_message(session, "assistant", summarize_draft(draft, preflight), kind="plan",
                                data={"plan": {"type": WORKFLOW_PLAN_TYPE, "draft": draft, "preflight": preflight}})
            return {"handled": True}
        _append_message(
            session, "assistant",
            "该计划已提交执行，进入只读状态：修改意见与重新规划不再套用（避免与"
            "已启动的 Run 冲突）。如需变更，请重新描述完整指令生成新计划。",
            data={"automation_run_id": str((session.get("active_run") or {}).get("automation_run_id") or "")},
        )
        return {"handled": True}
    version_bumped = False
    if not action:
        draft, applied = update_draft(draft, message)
        if not applied:
            return None
        # update_draft 命中修改规则时已递增 plan_version。
        version_bumped = True
        action = "replan"
        # 约束变更（含显式删除的约束重置）同步回会话级副本：后续模型
        # 重新生成计划（_make_workflow）以最新冻结约束为准，不得复活
        # 已撤回的要求（如「去掉 AOA」后的 aoa_requested）。
        if draft.get("user_constraints"):
            session["workflow_user_constraints"] = draft["user_constraints"]
    if action == "cancel":
        session["pending_plan"] = None
        # 审核修正：约束作用域绑定未提交计划——取消计划后冻结约束一并
        # 失效，下一次独立任务不被上一轮未执行的 AOA/CTS 要求污染。
        session["workflow_user_constraints"] = {}
        session["status"] = "idle"
        _append_step(session, "工作流计划已取消", "warning", "已保留对话记录；重新发送指令可再次生成计划")
        _append_message(session, "assistant", "工作流计划已取消。需要时重新描述指令即可再次生成计划。")
        return {"handled": True}
    if action == "replan":
        # 重新规划：保留已解析实体，刷新资源预检；裸 replan 在此递增版本，
        # 修改路径已由 update_draft 递增，不重复加。
        if not version_bumped:
            draft["plan_version"] = int(draft.get("plan_version") or 1) + 1
        preflight = refresh_workflow_preflight(session, draft, request=request)
        pending_plan["preflight"] = preflight
        _append_step(session, "重新规划", "done", f"计划 v{draft['plan_version']}，资源预检已刷新", {"plan": draft, "preflight": preflight})
        _append_message(
            session, "assistant", summarize_draft(draft, preflight),
            kind="plan",
            data={"plan": {"type": WORKFLOW_PLAN_TYPE, "draft": draft, "preflight": preflight}},
        )
        return {"handled": True}
    # status
    _append_message(session, "assistant", summarize_draft(draft, preflight), kind="plan",
                        data={"plan": {"type": WORKFLOW_PLAN_TYPE, "draft": draft, "preflight": preflight}})
    return {"handled": True}
