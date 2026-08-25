from __future__ import annotations

import os
from typing import Any

from features.cluster import get_cluster_service
from features.cluster.execution_spec import build_argv_from_spec
from foundation.responses import error_response, success_response


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
