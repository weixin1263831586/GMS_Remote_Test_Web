from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

from foundation.transport_contract import (
    TransportOperationError,
    execute_external_transport,
)
from worker_agent.process_inventory import discover_tradefed_processes


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NATIVE_ROOT = PROJECT_ROOT / "tools" / "gms-worker-native"
DIST_ROOT = NATIVE_ROOT / "dist" / platform.machine()
PROCESS_BINARY = DIST_ROOT / "gms-process-inventory"
USBIP_BINARY = DIST_ROOT / "gms-usbip-control"
FAKE_BIN = NATIVE_ROOT / "tests" / "fixtures" / "bin"

pytestmark = pytest.mark.skipif(
    not PROCESS_BINARY.is_file() or not USBIP_BINARY.is_file(),
    reason="matching prebuilt GMS Worker native tools are unavailable",
)


def _fake_proc_process(proc_root: Path) -> None:
    process = proc_root / "100"
    process.mkdir(parents=True)
    (proc_root / "uptime").write_text("1000.0 0.0\n", encoding="utf-8")
    (process / "stat").write_text(
        "100 (cts-tradefed) S 1 100 100 0 0 0 0 0 0 0 10 5 0 0 0 0 0 0 500\n",
        encoding="utf-8",
    )
    (process / "cmdline").write_bytes(
        b"/suite/tools/cts-tradefed\0CompatibilityConsole\0run\0cts\0-s\0SERIAL-1\0"
    )
    (process / "comm").write_text("cts-tradefed\n", encoding="utf-8")
    (process / "status").write_text("VmRSS:\t2048 kB\n", encoding="utf-8")
    os.symlink(proc_root, process / "cwd")


def test_native_process_inventory_reports_expected_accounting(tmp_path, monkeypatch):
    _fake_proc_process(tmp_path)
    monkeypatch.setenv("GMS_PROCESS_INVENTORY_BIN", str(PROCESS_BINARY))

    native_result = discover_tradefed_processes(
        [], proc_root=tmp_path, now=2000.0, stall_seconds=3600
    )

    assert native_result[0]["worker_job_id"] == "external-100-500"
    assert native_result[0]["devices"] == ["SERIAL-1"]
    assert native_result[0]["suite_type"] == "CTS"
    assert native_result[0]["rss_mb"] == 2.0


def test_native_usbip_reuses_existing_attachment(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_WORKER_USBIP_HELPER", "/fake/gms-worker-usbip")

    result = execute_external_transport(
        str(USBIP_BINARY),
        transport="usbip",
        action="attach",
        payload={
            "source_host": "192.0.2.10",
            "busids": ["1-2"],
            "adb_server_socket": "",
            "generation": 7,
        },
        timeout=10,
    )

    assert result["attached_busids"] == ["1-2"]
    assert result["already_attached_busids"] == ["1-2"]
    assert result["generation"] == 7
    assert result["readiness"] == "test_ready"
    assert result["devices"][0]["serial"] == "SERIAL-1"


def test_native_usbip_detach_uses_matching_port(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_WORKER_USBIP_HELPER", "/fake/gms-worker-usbip")

    result = execute_external_transport(
        str(USBIP_BINARY),
        transport="usbip",
        action="detach",
        payload={
            "source_host": "192.0.2.10",
            "busids": ["1-2"],
            "generation": 8,
        },
        timeout=10,
    )

    assert result["detached_ports"] == ["00"]
    assert result["transport_state"] == "disconnected"
    assert result["generation"] == 8


def test_native_usbip_rejects_invalid_host_with_structured_error():
    completed = subprocess.run(
        [str(USBIP_BINARY)],
        input=json.dumps({
            "schema_version": 1,
            "transport": "usbip",
            "action": "attach",
            "payload": {"source_host": "bad;host", "busids": ["1-2"]},
        }),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    response = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert response["success"] is False
    assert response["error"]["code"] == "TRANSPORT_EXECUTOR_INVALID_REQUEST"


def test_native_usbip_reports_port_query_failure(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_TEST_USBIP_MODE", "port_fail")

    with pytest.raises(TransportOperationError) as raised:
        execute_external_transport(
            str(USBIP_BINARY),
            transport="usbip",
            action="attach",
            payload={"source_host": "192.0.2.10", "busids": ["1-2"]},
            timeout=10,
        )

    assert raised.value.code == "USBIP_PORT_QUERY_FAILED"
    assert raised.value.retryable is True
    assert raised.value.details["context"] == "attach"


def test_native_usbip_reports_busy_export(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_TEST_USBIP_MODE", "busy")
    monkeypatch.setenv("GMS_USBIP_BUSY_RETRY_DELAY_MS", "0")

    with pytest.raises(TransportOperationError) as raised:
        execute_external_transport(
            str(USBIP_BINARY),
            transport="usbip",
            action="attach",
            payload={"source_host": "192.0.2.10", "busids": ["1-2"]},
            timeout=10,
        )

    assert raised.value.code == "USBIP_EXPORT_BUSY"
    assert raised.value.retryable is True
    assert raised.value.details["attach_errors"]["1-2"]["exit_code"] == 1


def test_native_usbip_reports_attach_failure(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_TEST_USBIP_MODE", "attach_fail")

    with pytest.raises(TransportOperationError) as raised:
        execute_external_transport(
            str(USBIP_BINARY),
            transport="usbip",
            action="attach",
            payload={"source_host": "192.0.2.10", "busids": ["1-2"]},
            timeout=10,
        )

    assert raised.value.code == "USBIP_ATTACH_FAILED"
    assert raised.value.retryable is True
    assert raised.value.details["attach_errors"]["1-2"]["message"] == (
        "source export unavailable"
    )


def test_native_usbip_rejects_attachment_that_disappears_after_success(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_TEST_USBIP_MODE", "unstable")
    monkeypatch.setenv("GMS_TEST_USBIP_STATE_FILE", str(tmp_path / "port_calls"))
    monkeypatch.setenv("GMS_USBIP_VERIFY_DELAY_MS", "0")

    with pytest.raises(TransportOperationError) as raised:
        execute_external_transport(
            str(USBIP_BINARY),
            transport="usbip",
            action="attach",
            payload={"source_host": "192.0.2.10", "busids": ["1-2"]},
            timeout=10,
        )

    assert raised.value.code == "USBIP_ATTACH_UNSTABLE"
    assert raised.value.retryable is True
    assert raised.value.details["missing_busids"] == ["1-2"]


def test_native_usbip_waits_for_delayed_port_visibility(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_TEST_USBIP_MODE", "delayed_port")
    monkeypatch.setenv("GMS_TEST_USBIP_STATE_FILE", str(tmp_path / "port_calls"))
    monkeypatch.setenv("GMS_USBIP_VERIFY_DELAY_MS", "0")

    result = execute_external_transport(
        str(USBIP_BINARY),
        transport="usbip",
        action="attach",
        payload={"source_host": "192.0.2.10", "busids": ["1-2"]},
        timeout=30,
    )

    assert result["attached_busids"] == ["1-2"]


def test_native_usbip_reports_partial_detach(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_TEST_USBIP_MODE", "detach_fail")

    with pytest.raises(TransportOperationError) as raised:
        execute_external_transport(
            str(USBIP_BINARY),
            transport="usbip",
            action="detach",
            payload={"source_host": "192.0.2.10", "busids": ["1-2"]},
            timeout=10,
        )

    assert raised.value.code == "USBIP_DETACH_PARTIAL"
    assert raised.value.retryable is True
    assert raised.value.details["detach_errors"]["00"]["exit_code"] == 1


@pytest.mark.parametrize(
    ("mode", "expected_code", "retryable"),
    [
        ("partial", "USBIP_ATTACH_PARTIAL", True),
        ("rollback_fail", "USBIP_ROLLBACK_FAILED", False),
    ],
)
def test_native_usbip_reports_atomic_rollback_status(
    tmp_path, monkeypatch, mode, expected_code, retryable,
):
    monkeypatch.setenv("PATH", f"{FAKE_BIN}:{os.environ['PATH']}")
    monkeypatch.setenv("GMS_TEST_USBIP_MODE", mode)
    monkeypatch.setenv("GMS_TEST_USBIP_STATE_FILE", str(tmp_path / "attached"))

    with pytest.raises(TransportOperationError) as raised:
        execute_external_transport(
            str(USBIP_BINARY),
            transport="usbip",
            action="attach",
            payload={
                "source_host": "192.0.2.10",
                "busids": ["1-2", "1-3"],
            },
            timeout=10,
        )

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable
    assert "1-3" in raised.value.details["attach_errors"]
    if mode == "partial":
        assert not (tmp_path / "attached").exists()
    else:
        assert "00" in raised.value.details["rollback_errors"]
