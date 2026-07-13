from __future__ import annotations

import os
from typing import Any

from features.cluster import get_cluster_service
from foundation.responses import error_response, success_response


def start_cluster_test(request: Any, client_id: str):
    """Translate the existing test form into a durable remote Worker job."""
    repository = get_cluster_service().repository
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
    data = {"worker_id": request.worker_id, "suite_key": selected_suite["suite_key"],
            "suite_path": tools_path, "devices": request.devices, "argv": cmd_parts,
            "env": {}, "owner_id": client_id, "source_type": "test-ui", "priority": 100}
    try:
        job = repository.create_job_with_leases(data)
        command = repository.create_command({
            "worker_id": request.worker_id, "command_type": "start_test",
            "job_id": job["id"], "attempt_id": job["current_attempt_id"],
            "payload": {"worker_job_id": f"wj-{job['id']}", "argv": cmd_parts,
                        "env": {}, "devices": request.devices},
        })
        repository.attach_command_to_job(job["id"], command)
        return success_response({"cluster_job_id": job["id"], "worker_id": request.worker_id},
                                message="Distributed test queued")
    except ValueError as exc:
        return error_response(str(exc), 409)
