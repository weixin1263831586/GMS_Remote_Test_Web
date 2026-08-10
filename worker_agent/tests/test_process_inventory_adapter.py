from __future__ import annotations

import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from foundation.native_tools import NativeToolUnavailableError
from worker_agent.process_inventory import (
    discover_tradefed_processes,
    process_inventory_capability_status,
)


def _record() -> dict:
    return {
        "worker_job_id": "external-10-20",
        "job_id": "",
        "attempt_id": "",
        "status": "running",
        "pid": 10,
        "devices": ["SERIAL"],
        "source": "external",
        "suite_type": "CTS",
        "command": "cts-tradefed run cts",
        "started_at": "2026-08-10T00:00:00+00:00",
        "elapsed_seconds": 10,
        "cpu_percent": 1.0,
        "rss_mb": 2.0,
        "process_count": 1,
        "log_path": "",
        "last_output_age_seconds": None,
        "warning": "",
    }


def _native_command(response: dict) -> str:
    script = (
        "import json,sys; request=json.load(sys.stdin); "
        "assert request['action']=='scan'; "
        f"print(json.dumps({response!r}))"
    )
    return shlex.join([sys.executable, "-c", script])


def test_process_inventory_uses_required_native_contract(tmp_path: Path):
    expected = [_record()]
    command = _native_command({
        "schema_version": 1,
        "success": True,
        "data": {"processes": expected},
    })

    with patch.dict("os.environ", {"GMS_PROCESS_INVENTORY_BIN": command}):
        result = discover_tradefed_processes(
            [{"pid": 99}], proc_root=tmp_path, now=100.0, stall_seconds=60
        )

    assert result == expected


def test_process_inventory_does_not_fallback_on_invalid_native_response(tmp_path: Path):
    command = _native_command({"schema_version": 1, "success": True, "data": {}})

    with patch.dict("os.environ", {"GMS_PROCESS_INVENTORY_BIN": command}), \
            pytest.raises(RuntimeError, match=r"data\.processes"):
        discover_tradefed_processes(proc_root=tmp_path, now=100.0)


def test_process_inventory_rejects_malformed_native_record(tmp_path: Path):
    command = _native_command({
        "schema_version": 1,
        "success": True,
        "data": {"processes": [{"worker_job_id": "incomplete"}]},
    })

    with patch.dict("os.environ", {"GMS_PROCESS_INVENTORY_BIN": command}), \
            pytest.raises(RuntimeError, match="invalid string fields"):
        discover_tradefed_processes(proc_root=tmp_path, now=100.0)


def test_process_inventory_capability_reports_missing_native_tool():
    with patch(
        "worker_agent.process_inventory.resolve_native_tool",
        side_effect=NativeToolUnavailableError("missing"),
    ):
        status = process_inventory_capability_status()

    assert status == {
        "backend": "unavailable",
        "available": False,
        "contract_version": 1,
    }
