"""Rust-only discovery adapter for managed and external Tradefed invocations."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from foundation.native_tools import (
    NativeToolUnavailableError,
    resolve_native_tool,
)


PROCESS_INVENTORY_SCHEMA_VERSION = 1


def process_inventory_capability_status() -> dict[str, Any]:
    try:
        command = resolve_native_tool(
            "GMS_PROCESS_INVENTORY_BIN", "gms-process-inventory"
        )
    except NativeToolUnavailableError:
        return {
            "backend": "unavailable",
            "available": False,
            "contract_version": PROCESS_INVENTORY_SCHEMA_VERSION,
        }
    return {
        "backend": "native",
        "available": True,
        "command": shlex.split(command)[0],
        "contract_version": PROCESS_INVENTORY_SCHEMA_VERSION,
    }


def _validate_processes(processes: Any) -> list[dict[str, Any]]:
    if not isinstance(processes, list) or not all(
        isinstance(item, dict) for item in processes
    ):
        raise RuntimeError("process inventory data.processes must be a JSON array")
    required_strings = {
        "worker_job_id", "job_id", "attempt_id", "status", "source",
        "suite_type", "command", "started_at", "log_path", "warning",
    }
    seen: set[str] = set()
    for item in processes:
        if any(not isinstance(item.get(field), str) for field in required_strings):
            raise RuntimeError("process inventory record has invalid string fields")
        worker_job_id = item["worker_job_id"]
        if not worker_job_id or worker_job_id in seen:
            raise RuntimeError("process inventory record ID is empty or duplicated")
        seen.add(worker_job_id)
        if not isinstance(item.get("pid"), int) or isinstance(item.get("pid"), bool):
            raise RuntimeError("process inventory record pid must be an integer")
        devices = item.get("devices")
        if not isinstance(devices, list) or not all(
            isinstance(device, str) for device in devices
        ):
            raise RuntimeError("process inventory record devices must be a string array")
        if not isinstance(item.get("process_count"), int) or isinstance(
            item.get("process_count"), bool
        ):
            raise RuntimeError("process inventory record process_count must be an integer")
        if not isinstance(item.get("elapsed_seconds"), int) or isinstance(
            item.get("elapsed_seconds"), bool
        ):
            raise RuntimeError("process inventory record elapsed_seconds must be an integer")
        for field in ("cpu_percent", "rss_mb"):
            value = item.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise RuntimeError(
                    f"process inventory record {field} must be numeric"
                )
        output_age = item.get("last_output_age_seconds")
        if output_age is not None and (
            not isinstance(output_age, int) or isinstance(output_age, bool)
        ):
            raise RuntimeError(
                "process inventory record last_output_age_seconds must be null or integer"
            )
    return processes


def discover_tradefed_processes(
    managed_jobs: list[dict[str, Any]] | None = None,
    *,
    proc_root: Path = Path("/proc"),
    now: float | None = None,
    stall_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Run the required native scanner and return its versioned result."""
    command = resolve_native_tool(
        "GMS_PROCESS_INVENTORY_BIN", "gms-process-inventory"
    )
    effective_now = time.time() if now is None else now
    effective_stall = stall_seconds or int(os.getenv("GMS_TF_STALL_SECONDS", "3600"))
    request = {
        "schema_version": PROCESS_INVENTORY_SCHEMA_VERSION,
        "action": "scan",
        "payload": {
            "managed_pids": [
                int(item["pid"])
                for item in (managed_jobs or [])
                if item.get("pid") is not None
            ],
            "proc_root": str(proc_root),
            "now": effective_now,
            "stall_seconds": effective_stall,
        },
    }
    timeout = max(
        1,
        min(int(os.getenv("GMS_PROCESS_INVENTORY_TIMEOUT_SECONDS", "10")), 60),
    )
    completed = subprocess.run(
        shlex.split(command),
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or "").strip()[-2000:]
        raise RuntimeError(
            f"process inventory exited with code {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    try:
        response = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError("process inventory returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise RuntimeError("process inventory response must be a JSON object")
    if int(response.get("schema_version") or 0) != PROCESS_INVENTORY_SCHEMA_VERSION:
        raise RuntimeError("process inventory contract version is incompatible")
    if response.get("success") is not True:
        error = response.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(str(message or "native process inventory scan failed"))
    data = response.get("data") or {}
    processes = data.get("processes") if isinstance(data, dict) else None
    return _validate_processes(processes)
