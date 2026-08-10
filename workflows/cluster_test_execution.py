from __future__ import annotations

import os
from typing import Any

from features.cluster import get_cluster_service
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
    parts = os.path.normpath(tools_path).split(os.sep)
    try:
        root_index = next(i for i, part in enumerate(parts) if part == "GMS-Suite")
        suite_root = os.sep.join(parts[:root_index + 1]) or os.sep
    except StopIteration:
        suite_root = os.path.dirname(os.path.dirname(os.path.dirname(tools_path)))
    script = os.path.join(suite_root, "run_GMS_Test_Auto.sh")
    serials = [item.split(":", 1)[1] if item.startswith(f"{request.worker_id}:") else item
               for item in request.devices]
    cmd_parts = [script, (request.test_type or selected_suite["suite_type"]).lower()]
    if request.retry_dir:
        cmd_parts.extend(["retry", os.path.basename(request.retry_dir.rstrip("/"))])
    else:
        if request.test_module:
            cmd_parts.append(request.test_module)
        if request.test_case:
            cmd_parts.append(request.test_case)
    device_args = []
    if len(serials) > 1:
        device_args.extend(["--shard-count", str(len(serials))])
    for serial in serials:
        device_args.extend(["-s", serial])
    cmd_parts.extend(["--device-args", " ".join(device_args), "--test-suite", tools_path])
    if request.local_server:
        cmd_parts.extend(["--local-server", request.local_server])
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
    data["execution_spec"] = {
        "test_type": (request.test_type or selected_suite["suite_type"]).lower(),
        "suite_path": tools_path,
        "module": request.test_module,
        "test_case": request.test_case,
        "retry_dir": os.path.basename(request.retry_dir.rstrip("/")) if request.retry_dir else "",
        "devices": serials,
        "local_server": request.local_server,
        "no_retry": False,
        "copy_remote": bool(request.local_server),
    }
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
