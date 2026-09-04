from __future__ import annotations

import os
import re
from typing import Any

from features.cluster import get_cluster_service
from features.cluster.execution_spec import build_argv_from_spec
from foundation.responses import error_response, success_response


# 与 features/test_execution/suite_modules.py 的 MODULE_EXTENSIONS 保持一致；
# 这里独立复制以避免 workflow 层反向依赖 features.test_execution 的运行时。
_MODULE_EXTENSIONS = (".apk", ".jar", ".config", ".xml")
_INSTRUMENTATION_PACKAGE_RE = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
)


def _suite_testcases_dir(tools_path: str) -> str:
    root = str(tools_path or "").rstrip("/")
    if root.endswith("/tools"):
        root = root[: -len("/tools")]
    return os.path.join(root, "testcases")


def _list_suite_modules(testcases_dir: str) -> dict[str, str]:
    """Map tradefed module name -> testcases file (bounded local walk)."""
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


def _resolve_test_module(tools_path: str, module: str) -> str:
    """Validate test_module against the suite's local testcases directory.

    Returns an error message ("" when the module is acceptable). Only runs
    when the testcases directory exists on the local filesystem (local
    Worker); remote Worker suites are passed through unchanged.
    """
    module = str(module or "").strip()
    if not module:
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
        existing = repository.get_job_by_automation_run(request.automation_run_id)
        if existing:
            if existing.get("owner_id") != owner_id:
                return error_response("Automation job belongs to another owner", 409)
            return success_response({
                "cluster_job_id": existing["id"],
                "attempt_id": existing["current_attempt_id"],
                "worker_id": existing["assigned_worker_id"],
                "deduplicated": True,
            }, message="Distributed test already queued")
    if reservation and reservation.get("status") != "active":
        return error_response("Device reservation is missing or expired", 409)
    suites = repository.list_suites(request.worker_id)
    selected_suite = next((item for item in suites if item["tools_path"] == request.test_suite
                           and item["available"]), None)
    if not selected_suite:
        return error_response("Selected suite is not available on the Worker", 409)
    tools_path = selected_suite["tools_path"]
    module_error = _resolve_test_module(tools_path, request.test_module)
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
    except Exception as exc:  # HTTPException from the shared builder
        detail = getattr(exc, "detail", None) or str(exc)
        return error_response(str(detail), 400)
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
        command = repository.create_command({
            "worker_id": request.worker_id, "command_type": "start_test",
            "job_id": job["id"], "attempt_id": job["current_attempt_id"],
            "operation_id": f"{job['current_attempt_id']}:start_test",
            "payload": {"worker_job_id": f"wj-{job['id']}", "argv": cmd_parts,
                        "execution_spec": data["execution_spec"],
                        "env": {}, "devices": request.devices,
                        "trace_id": job.get("trace_id", ""),
                        "lease_tokens": [{
                            "lease_id": lease["id"],
                            "device_id": lease["device_id"],
                            "generation": lease["generation"],
                            "attempt_id": lease["attempt_id"],
                        } for lease in job.get("leases") or []
                            if lease.get("status") == "active"]},
        })
        repository.attach_command_to_job(job["id"], command)
        return success_response({"cluster_job_id": job["id"],
                                 "attempt_id": job["current_attempt_id"],
                                 "worker_id": request.worker_id},
                                message="Distributed test queued")
    except ValueError as exc:
        return error_response(str(exc), 409)
