"""Interrupted USB-debug recovery retains state without replaying toggle clicks."""
from unittest.mock import Mock

import pytest

from worker_agent import aoa_actions, device_actions
from worker_agent.aoa_diagnostics import confirm_usb_restore, failure_detail, inspect_failed_adb, record_failure
from worker_agent.aoa_usb import bound_port


def failure():
    error = aoa_actions.AoaError("AOAv2-compatible device not found: ABC")
    record_failure(error, index=63, total=71, command="click 180 280", completed=62,
                   last_input=(61, "click 320 320"))
    return error


@pytest.mark.parametrize("state", ["device", "unauthorized", "absent", "identity_mismatch"])
def test_post_failure_state_is_evidence_never_success(state):
    error = failure()
    observe = Mock(return_value={"adb_recovered": state == "device", "adb_state": state})
    inspect_failed_adb(error, observe=observe, suppressed=lambda: False, validate=Mock())
    result = error.aoa_result
    assert result["success"] is False and result["commands_executed"] == 62
    assert result["adb_state"] == state
    assert "63/71" in failure_detail(result)
    assert "61 条 click 320 320" in failure_detail(result)
    if state == "unauthorized":
        assert "RSA" in failure_detail(result) and "不要再次点击" in failure_detail(result)
    observe.assert_called_once_with()


@pytest.mark.parametrize("blocker", ["suppressed", "cancelled", "fencing"])
def test_diagnostics_never_restart_adb_under_suppression_or_lost_claim(blocker):
    error = failure()
    error.cancelled = blocker == "cancelled"
    observe = Mock()
    validate = Mock(side_effect=RuntimeError("claim lost") if blocker == "fencing" else None)
    inspect_failed_adb(error, observe=observe, suppressed=lambda: blocker == "suppressed", validate=validate)
    observe.assert_not_called()
    assert "adb_state" not in error.aoa_result


def test_failed_probe_retains_original_error():
    error = failure()
    inspect_failed_adb(error, observe=Mock(side_effect=TimeoutError("adb timed out")),
                       suppressed=lambda: False, validate=Mock())
    assert "AOAv2-compatible" in str(error)
    assert error.aoa_result["failed_index"] == 63


@pytest.mark.parametrize("wait_seconds", [0, 60])
def test_device_action_keeps_port_binding_and_receipt_after_usb_cleanup(monkeypatch, wait_seconds):
    error = failure()
    trace = []
    monkeypatch.setattr(device_actions, "probe_devices", lambda: [{
        "serial": "ABC", "state": "usb_only", "transport": "local_usb", "properties": {"usb": "1-6"}}])

    def run(*args, **kwargs):
        trace.append("usb_cleanup_and_suppression_end")
        raise error

    def observe(serial, seconds):
        assert serial == "ABC" and seconds == 0 and bound_port() == "1-6"
        trace.append("inspect_adb")
        return {"adb_state": "unauthorized", "adb_recovered": False}

    monkeypatch.setattr(aoa_actions, "run_aoa_sequence", run)
    monkeypatch.setattr(aoa_actions, "_wait_adb_state", observe)
    monkeypatch.setattr("worker_agent.adb_suppression.adb_daemon_suppressed", lambda: False)
    with pytest.raises(RuntimeError) as caught:
        device_actions.execute_device_action("aoa_sequence", ["ABC"], {
            "aoa_commands": ["wake"], "wait_adb_seconds": wait_seconds, "_validate_fencing": Mock()})
    assert caught.value.aoa_result["failed_index"] == 63
    assert "本条尚未发送 HID" in str(caught.value)
    assert trace == ["usb_cleanup_and_suppression_end"] + (["inspect_adb"] if wait_seconds else [])
    if wait_seconds:
        assert "RSA" in str(caught.value)


def test_diagnostics_on_setup_failure_do_not_claim_a_command_was_sent():
    error = aoa_actions.AoaError("cannot configure accessory")
    record_failure(error, index=0, total=71, command="", completed=0)
    assert failure_detail(error.aoa_result) == ""
    assert error.aoa_result["failed_input_state"] == "not_sent"


def completed_cleanup():
    return {"commands_total": 71, "commands_executed": 71, "adb_recovered": True,
            "adb_state": "device", "native_mode_restored": False, "reconcile_required": True,
            "cleanup_errors": [{"step": "reset_usb", "error": "[Errno 2] Entity not found"},
                               {"step": "restore_native_mode", "error": "restore_native_mode did not complete"}]}


def test_native_usb_and_authorized_adb_resolve_stale_cleanup_handle():
    result = completed_cleanup()
    errors = list(result["cleanup_errors"])
    observe = Mock(return_value=True)
    confirm_usb_restore(result, observe)
    assert result["native_mode_restored"] is True and result["reconcile_required"] is False
    assert result["cleanup_errors"] == [] and result["cleanup_warnings"] == errors
    observe.assert_called_once_with()


@pytest.mark.parametrize("change", [
    {"adb_recovered": False, "adb_state": "unauthorized"},
    {"commands_executed": 62, "failed_index": 63},
    {"cleanup_errors": [{"step": "release_touch", "error": "HID release failed"}]},
    {"cleanup_errors": [{"step": "dispose_usb", "error": "handle still open"}]},
])
def test_incomplete_input_or_other_cleanup_errors_never_become_warnings(change):
    result = {**completed_cleanup(), **change}
    observe = Mock(return_value=True)
    before = dict(result)
    confirm_usb_restore(result, observe)
    assert result == before
    observe.assert_not_called()


def test_accessory_adb_does_not_prove_native_usb_restored():
    result = completed_cleanup()
    confirm_usb_restore(result, lambda: False)
    assert result["native_mode_restored"] is False and result["reconcile_required"] is True
    assert len(result["cleanup_errors"]) == 2 and "cleanup_warnings" not in result


def test_lost_usb_identity_or_claim_does_not_resolve_cleanup():
    result = completed_cleanup()
    with pytest.raises(RuntimeError, match="identity changed"):
        confirm_usb_restore(result, Mock(side_effect=RuntimeError("USB identity changed")))
    assert result["native_mode_restored"] is False and result["cleanup_errors"]
