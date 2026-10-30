from __future__ import annotations

import logging
import os
import re
from typing import Any

from fastapi import HTTPException

from features.cluster import build_argv_from_spec, get_cluster_service, request_fingerprint
from foundation.cache import TTLCache
from foundation.error_model import ApiError
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)


# 与 features/test_execution/suite_modules.py 的 MODULE_EXTENSIONS 保持一致；
# 这里独立复制以避免 workflow 层反向依赖 features.test_execution 的运行时。
_MODULE_EXTENSIONS = (".apk", ".jar", ".config", ".xml")
_INSTRUMENTATION_PACKAGE_RE = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
)

# 套件 testcases 目录含数千文件，逐任务全量遍历代价高；模块清单在进程内
# 短 TTL 缓存（套件目录在任务间极少变化）。
_SUITE_MODULES_CACHE_TTL_SECONDS = 30.0
_suite_modules_cache = TTLCache[str, dict[str, str]](
    _SUITE_MODULES_CACHE_TTL_SECONDS, max_entries=32,
)


def _suite_testcases_dir(tools_path: str) -> str:
    root = str(tools_path or "").rstrip("/")
    if root.endswith("/tools"):
        root = root[: -len("/tools")]
    return os.path.join(root, "testcases")


def _list_suite_modules(testcases_dir: str) -> dict[str, str]:
    """Return an isolated module mapping, reusing one scan per directory/TTL."""
    cache_key = os.path.realpath(os.path.expanduser(testcases_dir))
    return dict(_suite_modules_cache.get_or_load(cache_key, lambda: _scan_suite_modules(cache_key)))


def _scan_suite_modules(testcases_dir: str) -> dict[str, str]:
    modules: dict[str, str] = {}
    for current, dirs, files in os.walk(testcases_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            lower = name.lower()
            for ext in _MODULE_EXTENSIONS:
                if lower.endswith(ext):
                    modules.setdefault(name[: -len(ext)], os.path.join(current, name))
                    break
    return modules


def _module_suggestions(requested: str, modules: dict[str, str]) -> list[str]:
    tokens = [
        token for token in re.split(r"[._\-]+", requested.lower())
        if len(token) >= 3
    ]

    def score(name: str) -> int:
        lowered = name.lower()
        parts = set(re.split(r"[._\-]+", lowered))
        total = 0
        for token in tokens:
            if token in parts:
                total += 2
            elif token in lowered:
                total += 1
        return total

    ranked = sorted(
        (name for name in modules if score(name) > 0),
        key=lambda name: (-score(name), name),
    )
    return ranked[:5]


def _resolve_test_module(tools_path: str, module: str, worker_id: str = "") -> str:
    """Validate test_module against the suite's local testcases directory.

    Returns an error message ("" when the module is acceptable).  Only runs
    for the LOCAL Worker and only when the testcases directory exists on the
    local filesystem.  A remote Worker may have a suite at the same
    path with different modules — validating it against the Controller's
    local files rejected valid remote modules (HTTP 400).  Remote Worker
    suites are passed through unchanged; the Worker executes what its own
    inventory advertises.
    """
    module = str(module or "").strip()
    if not module:
        return ""
    if worker_id:
        from foundation.cluster_port import get_local_worker_id

        if str(worker_id) != get_local_worker_id():
            return ""
    testcases_dir = _suite_testcases_dir(tools_path)
    if not os.path.isdir(testcases_dir):
        return ""
    modules = _list_suite_modules(testcases_dir)
    if module in modules:
        return ""
    hint = ""
    suggestions = _module_suggestions(module, modules)
    if suggestions:
        hint = "；最接近的 tradefed 模块: " + ", ".join(suggestions)
    if _INSTRUMENTATION_PACKAGE_RE.match(module):
        return (
            f"test_module '{module}' 看起来是 instrumentation 包名"
            f"（apk 内的 java package），不是 tradefed 模块名，"
            f"套件 testcases 中没有同名模块{hint}。"
            "请改用 tradefed 模块名（如 CtsHardwareTestCases）后重试；"
            "可用 GET /api/test/suites/modules?query=<关键词> 查询模块名。"
        )
    return (
        f"test_module '{module}' 在所选套件的 testcases 目录中不存在{hint}。"
        "请确认模块名后重试；"
        "可用 GET /api/test/suites/modules?query=<关键词> 查询模块名。"
    )


def start_cluster_test(request: Any, client_id: str):
    """Translate the existing test form into a durable remote Worker job."""
    repository = get_cluster_service().repository
    owner_id = client_id
    # automation_run_id（含 agent 的 agent-op:<operation_id>）是幂等去重键。
    # 去重检查与任务创建必须在同一把跨进程 guard 内完成（ADR 0016 的
    # job_request_guard / cluster_job_requests 回执表）：否则并发同 ID
    # 请求都可能在创建前查不到既有任务而各自建 Job。回执与任务同事务
    # 持久化；request_hash 保证同一 ID 换参数时显式冲突而不是静默复用。
    if request.automation_run_id:
        # 指纹覆盖所有影响任务行为/权限/关联的参数（复审 P2）：核心执行
        # 参数 + 设备预约（权限边界）+ 构建/Gerrit/Redmine 关联（报告
        # 归属）。local_server 等纯展示字段不纳入。
        fingerprint = request_fingerprint({
            "worker_id": request.worker_id, "test_type": request.test_type,
            "test_suite": request.test_suite, "test_module": request.test_module,
            "test_case": request.test_case, "retry_dir": request.retry_dir,
            "devices": list(request.devices),
            "device_reservation_id": request.device_reservation_id,
            "build_id": request.build_id,
            "build_artifact_id": request.build_artifact_id,
            "gerrit_change_id": request.gerrit_change_id,
            "gerrit_patchset": request.gerrit_patchset,
            "redmine_issue_id": request.redmine_issue_id,
        })
        try:
            with repository.job_request_guard(owner_id, request.automation_run_id):
                return _start_cluster_test_guarded(
                    request, client_id, fingerprint
                )
        except ApiError as exc:
            return exc.to_response()
    return _start_cluster_test_guarded(request, client_id, None)


def _start_cluster_test_guarded(
    request: Any, client_id: str, fingerprint: str | None,
):
    repository = get_cluster_service().repository
    owner_id = client_id
    reservation = None
    if request.device_reservation_id:
        reservation = repository.get_reservation(request.device_reservation_id)
        if not reservation:
            return error_response("Device reservation is missing or expired", 409)
        if reservation.get("owner_id") != owner_id:
            return error_response("Device reservation belongs to another owner", 409)
        if request.automation_run_id and reservation.get("source_id") != request.automation_run_id:
            return error_response("Device reservation belongs to another automation run", 409)
    if request.automation_run_id:
        # 回执优先（原子去重）；哈希不符（同 ID 换参数）抛 409 不吞。
        # 无回执时回退 request_json 扫描，兜住历史上创建的无回执任务。
        existing = repository.find_job_request(
            owner_id, request.automation_run_id, fingerprint or ""
        )
        if existing is None:
            existing = repository.get_job_by_automation_run(request.automation_run_id)
            if existing and existing.get("owner_id") == owner_id and fingerprint:
                # 历史无回执任务：语义级比对关键字段，同 ID 换参数显式冲突。
                prior = existing.get("request") or {}
                if any(
                    str(prior.get(key) or "") != str(value or "")
                    for key, value in {
                        "worker_id": request.worker_id,
                        "suite_path": request.test_suite,
                        "test_module": request.test_module,
                        "test_case": request.test_case,
                        "retry_dir": request.retry_dir,
                        "device_reservation_id": request.device_reservation_id,
                        "build_id": request.build_id,
                        "build_artifact_id": request.build_artifact_id,
                        "gerrit_change_id": request.gerrit_change_id,
                        "gerrit_patchset": request.gerrit_patchset,
                        "redmine_issue_id": request.redmine_issue_id,
                    }.items()
                ) or list(prior.get("devices") or []) != list(request.devices) or (
                    request.test_type and request.test_type.lower() != str(
                        (prior.get("execution_spec") or {}).get("test_type")
                        or prior.get("test_type") or ""
                    ).lower()
                ):
                    return ApiError.conflict(
                        "automation_run_id 已绑定不同参数的既有任务，请更换 operation_id 或复用原参数",
                    ).to_response()
        if existing:
            if existing.get("owner_id") != owner_id:
                return error_response("Automation job belongs to another owner", 409)
            # 崩溃窗口补偿：Job+回执已提交而 start_test 派发命令未写时
            # （Controller 在两段提交之间退出），直接返回 deduplicated
            # 会留下一个永远不会执行的 Job。replay_job_command 恢复
            # 初始派发缺口（同一契约：/api/cluster/jobs 的重放路径）；
            # 非 initial 态由其返回真实命令或 None。
            try:
                replayed = repository.replay_job_command(existing)
            except ApiError as exc:
                return exc.to_response()
            message = (
                "Distributed test already queued"
                if replayed is not None
                else "Distributed test already exists"
            )
            return success_response({
                "cluster_job_id": existing["id"],
                "attempt_id": existing["current_attempt_id"],
                "worker_id": existing["assigned_worker_id"],
                "deduplicated": True,
            }, message=message)
    if reservation and reservation.get("status") != "active":
        return error_response("Device reservation is missing or expired", 409)
    suites = repository.list_suites(request.worker_id)
    selected_suite = next((item for item in suites if item["tools_path"] == request.test_suite
                           and item["available"]), None)
    if not selected_suite:
        return error_response("Selected suite is not available on the Worker", 409)
    tools_path = selected_suite["tools_path"]
    module_error = _resolve_test_module(tools_path, request.test_module, request.worker_id)
    if module_error:
        return error_response(module_error, 400)
    serials = [item.split(":", 1)[1] if item.startswith(f"{request.worker_id}:") else item
               for item in request.devices]
    # argv 只能从 ExecutionSpec 派生：这里与 API 路径共用同一个 builder，
    # 避免 workflow 与 features.cluster.execution_spec 出现两套拼装逻辑
    # （spec 校验更新后 workflow 被遗忘、Automation 绕过校验）。
    execution_spec = {
        "test_type": (request.test_type or selected_suite["suite_type"]).lower(),
        "suite_path": tools_path,
        "module": request.test_module,
        "test_case": request.test_case,
        "retry_dir": os.path.basename(request.retry_dir.rstrip("/")) if request.retry_dir else "",
        "devices": serials,
        "local_server": request.local_server,
        "no_retry": False,
        "copy_remote": False,
    }
    try:
        cmd_parts = build_argv_from_spec(execution_spec)
    except HTTPException as exc:
        # 共享 builder 只把 ExecutionSpecError 适配为 HTTPException（携带
        # 原始 status_code）；其它异常属于编程错误，不得吞成 4xx。
        return error_response(str(exc.detail), exc.status_code)
    # Transport compatibility applies to EVERY job entry point:
    # /api/cluster/jobs runs this check via jobs_api, but /api/test/start
    # previously skipped it, letting ADB Proxy devices receive tests that
    # require a physical USB/Fastboot channel.
    from features.devices import incompatible_test_devices

    inventory = {
        str(item.get("serial") or ""): item
        for item in repository.list_devices(request.worker_id)
    }
    selected_inventory = []
    for device_id in request.devices:
        serial = str(device_id or "")
        prefix = f"{request.worker_id}:"
        if serial.startswith(prefix):
            serial = serial[len(prefix):]
        if serial in inventory:
            selected_inventory.append(inventory[serial])
    incompatible, policy = incompatible_test_devices(
        selected_inventory,
        cmd_parts,
        {},
    )
    if incompatible:
        return error_response(
            "所选测试需要真实USB/Fastboot通道，不能使用ADB Proxy设备: "
            + ", ".join(incompatible)
            + "；请改用USB/IP或在设备来源Worker本地执行。"
            + f"原因：{policy['reason']}",
            409,
        )
    full_suite = not request.retry_dir and not request.test_module and not request.test_case
    required_memory_gb = (float(os.getenv("GMS_CTS_FULL_MEMORY_GB", "28"))
                          if full_suite and (request.test_type or selected_suite["suite_type"]).lower() == "cts"
                          else 0)
    data = {"worker_id": request.worker_id, "suite_key": selected_suite["suite_key"],
            "suite_path": tools_path, "devices": request.devices, "argv": cmd_parts,
            "env": {}, "owner_id": owner_id,
            "source_type": "automation" if request.automation_run_id else "test-ui", "priority": 100,
            "exclusive_host": full_suite, "required_memory_gb": required_memory_gb,
            "test_module": request.test_module, "test_case": request.test_case,
            "retry_dir": request.retry_dir}
    data["execution_spec"] = execution_spec
    if fingerprint is not None:
        # 回执与任务同一事务提交（create_job_with_leases 内部处理）：
        # 崩溃后要么两者都在（find_job_request 命中），要么都不在（重试
        # 走创建），不存在"任务在、回执丢"的半状态。
        data["_idempotency_key"] = request.automation_run_id
        data["_request_hash"] = fingerprint
    data.update({
        "automation_run_id": request.automation_run_id,
        "device_reservation_id": request.device_reservation_id,
        "build_id": request.build_id,
        "build_artifact_id": request.build_artifact_id,
        "gerrit_change_id": request.gerrit_change_id,
        "gerrit_patchset": request.gerrit_patchset,
        "redmine_issue_id": request.redmine_issue_id,
    })
    try:
        job = repository.create_job_with_leases(data)
    except ValueError as exc:
        return error_response(str(exc), 409)
    try:
        # Shared dispatch helper (idempotent per attempt) with the
        # same compensation contract as /api/cluster/jobs.
        command = repository.dispatch_job_start_command(
            job,
            argv=cmd_parts,
            execution_spec=data["execution_spec"],
            env={},
            devices=request.devices,
        )
        del command
        return success_response({"cluster_job_id": job["id"],
                                 "attempt_id": job["current_attempt_id"],
                                 "worker_id": request.worker_id},
                                message="Distributed test queued")
    except Exception as exc:
        # Job and dispatch command are committed separately.  Without
        # compensation, a command-write failure left an `assigned` job with
        # zero dispatchable commands and active claims; no watchdog covered
        # that state.  Fail the job and release its claims so callers can
        # retry cleanly.
        # See docs/architecture/adr/0001-controller-worker-boundary.md.
        logger.exception(
            "Dispatch command commit failed for job %s; compensating", job["id"]
        )
        repository.compensate_failed_dispatch(job["id"], exc, attempt_id=job["current_attempt_id"])
        return error_response(
            "任务已创建但派发命令写入失败，请稍后重试（任务已回滚为失败状态）", 503
        )
