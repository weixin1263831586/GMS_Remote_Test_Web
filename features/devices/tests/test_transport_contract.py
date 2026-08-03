import shlex
import sys
from unittest.mock import MagicMock, patch

import pytest

from features.devices.transport_policy import (
    incompatible_test_devices,
)
from features.devices.transport_policy import (
    test_transport_requirement as classify_transport_requirement,
)
from features.devices.transport_registry import build_transport_records
from foundation.network_quality import probe_tcp_quality
from foundation.transport_contract import (
    TransportOperationError,
    execute_external_transport,
)
from worker_agent.transport_cli import execute_request


def _executor_command(response: dict, exit_code: int = 0) -> str:
    script = (
        "import json,sys; json.load(sys.stdin); "
        f"print(json.dumps({response!r})); sys.exit({exit_code})"
    )
    return shlex.join([sys.executable, "-c", script])


def test_external_transport_uses_versioned_json_contract():
    command = _executor_command({
        "schema_version": 1,
        "success": True,
        "transport_state": "attached",
        "protocol_state": "adb",
        "readiness": "test_ready",
        "generation": 7,
        "data": {"attached_busids": ["1-1"]},
    })

    result = execute_external_transport(
        command,
        transport="usbip",
        action="attach",
        payload={"busids": ["1-1"], "generation": 7},
        timeout=5,
    )

    assert result["attached_busids"] == ["1-1"]
    assert result["transport_contract_version"] == 1
    assert result["generation"] == 7


def test_external_transport_preserves_structured_failure():
    command = _executor_command({
        "schema_version": 1,
        "success": False,
        "error": {
            "code": "USBIP_TCP_UNREACHABLE",
            "message": "port closed",
            "retryable": True,
            "remediation": "open TCP 3240",
        },
    })

    with pytest.raises(TransportOperationError) as raised:
        execute_external_transport(
            command,
            transport="usbip",
            action="attach",
            payload={},
            timeout=5,
        )

    assert raised.value.code == "USBIP_TCP_UNREACHABLE"
    assert raised.value.retryable is True


def test_reference_transport_cli_rejects_unknown_transport():
    with pytest.raises(TransportOperationError) as raised:
        execute_request({
            "schema_version": 1,
            "transport": "unknown",
            "action": "status",
            "payload": {},
        })

    assert raised.value.code == "TRANSPORT_EXECUTOR_INVALID_REQUEST"


def test_tcp_quality_reports_partial_loss_and_jitter():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    with patch(
        "foundation.network_quality.socket.create_connection",
        side_effect=[connection, OSError("timeout"), connection, connection],
    ), patch(
        "foundation.network_quality.time.monotonic",
        side_effect=[0, 0.01, 1, 2, 2.03, 3, 3.05],
    ):
        result = probe_tcp_quality("192.0.2.10", 3240)

    assert result["reachable"] is True
    assert result["loss_percent"] == 25.0
    assert result["rating"] == "poor"


def test_fastboot_and_usb_modules_reject_adb_proxy_transport():
    devices = [{"serial": "PROXY1", "transport": "adb_proxy"}]

    unsupported, policy = incompatible_test_devices(
        devices,
        ["cts-tradefed", "run", "commandAndExit", "cts", "-m", "CtsUsbTests"],
    )

    assert unsupported == ["PROXY1"]
    assert policy["requirement"] == "physical_usb"
    assert classify_transport_requirement(["gts-tradefed", "run", "gts"])[
        "requirement"
    ] == "adb"


def test_transport_registry_is_a_normalized_read_only_projection():
    records = build_transport_records(
        adb_proxy_assignments=[{
            "source_worker_id": "source",
            "target_worker_id": "target",
            "devices": ["SERIAL1"],
            "status": "connected",
            "generation": 4,
        }],
        usbip_assignments=[{
            "device_host": "user@source",
            "worker_id": "target",
            "busid": "1-2",
            "device_serials": ["SERIAL2"],
            "status": "attached",
            "generation": 9,
        }],
    )

    assert [(item["transport"], item["device_id"]) for item in records] == [
        ("adb_proxy", "SERIAL1"),
        ("usbip", "SERIAL2"),
    ]
    assert records[1]["source_identity"]["busid"] == "1-2"
