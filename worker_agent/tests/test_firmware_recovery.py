"""A successful flash must not mask failed ADB recovery or repeat HID input."""
from unittest.mock import Mock, patch

import pytest

from worker_agent.firmware_recovery import (
    recover_after_flash,
    recovery_options,
    validate_preflash,
    wait_for_boot,
)


def snapshot():
    import hashlib
    import json

    commands = ["click 180 450", "sleep 2000"]
    return {"commands": commands, "commands_sha256": hashlib.sha256(json.dumps(
        commands, separators=(",", ":")).encode()).hexdigest(),
        "usb_port_path": "1-6", "wait_adb_seconds": 60, "boot_wait_seconds": 0}


def device(port="1-6", state="usb_only", serial="ABC"):
    return {"serial": serial, "state": state, "transport": "local_usb", "properties": {"usb": port}}


def test_post_flash_recovery_keeps_frozen_hid_mode_and_settle_time():
    frozen = snapshot()
    frozen["metadata"] = {"hid_touch_mode": "direct", "hid_settle_seconds": 4, "hid_session_mode": "per_command"}
    options = recovery_options(frozen)
    assert options["aoa_hid_touch_mode"] == "direct"
    assert options["aoa_hid_settle_seconds"] == 4
    assert options["aoa_hid_session_mode"] == "per_command"
    assert options["aoa_commands"] == frozen["commands"]


def test_recovery_requires_authorized_adb_not_just_successful_input():
    execute = Mock(return_value={"success": True, "adb_recovered": False, "adb_state": "unauthorized"})
    result = recover_after_flash({"success": True}, snapshot(), ["w:ABC"], Mock(), execute)
    assert result["success"] is False and result["flash_success"] is True
    assert result["stage"] == "AOA_RECOVERY_FAILED"
    assert "无需重新烧写" in result["error"]
    assert execute.call_count == 1


def test_successful_recovery_preserves_flash_result_and_fencing():
    validate = Mock()
    execute = Mock(return_value={"success": True, "adb_recovered": True})
    result = recover_after_flash({"success": True, "exit_code": 0}, snapshot(), ["w:ABC"], validate, execute)
    assert result["success"] is True and result["stage"] == "ADB_READY"
    assert result["exit_code"] == 0
    assert execute.call_args.args[2]["_validate_fencing"] is validate


def test_mid_sequence_disconnect_is_never_retried():
    execute = Mock(side_effect=RuntimeError("device vanished; input was not replayed"))
    result = recover_after_flash({"success": True}, snapshot(), ["w:ABC"], Mock(), execute)
    assert result["reconcile_required"] is True
    assert "device vanished" in result["error"]
    execute.assert_called_once()


def test_failed_flash_never_sends_hid():
    execute = Mock()
    result = recover_after_flash({"success": False}, snapshot(), ["w:ABC"], Mock(), execute)
    assert result == {"success": False}
    execute.assert_not_called()


def test_changed_snapshot_rejected_before_any_input():
    changed = snapshot()
    changed["commands"].append("home")
    with pytest.raises(ValueError, match="snapshot changed"):
        recovery_options(changed)


def test_preflash_port_change_rejected():
    with pytest.raises(ValueError, match="identity changed"):
        validate_preflash(snapshot(), ["w:ABC"], Mock(), lambda: [device("1-7")])


def test_boot_wait_rejects_wrong_port_without_sending_input():
    with pytest.raises(RuntimeError, match="identity changed"):
        wait_for_boot(snapshot(), ["w:ABC"], Mock(), lambda: [device("1-7")])


def test_boot_wait_requires_android_on_original_serial_and_port():
    validate = Mock()
    probe = Mock(side_effect=[[device(state="fastboot")], [], [device(serial="OTHER")], [device()]])
    events = []
    with patch("worker_agent.firmware_recovery.time.sleep"):
        wait_for_boot(snapshot(), ["w:ABC"], validate, probe, events.append)
    assert probe.call_count == 4
    assert validate.call_count == 4
    assert events[0]["phase"] == "waiting_boot"


def test_expired_claim_stops_boot_wait_immediately():
    validate = Mock(side_effect=RuntimeError("claim changed"))
    probe = Mock()
    with pytest.raises(RuntimeError, match="claim changed"):
        wait_for_boot(snapshot(), ["w:ABC"], validate, probe)
    probe.assert_not_called()


def test_boot_wait_timeout_is_bounded():
    with patch("worker_agent.firmware_recovery.time.monotonic", side_effect=[0, 181]), pytest.raises(
        RuntimeError, match="原 USB 端口"):
        wait_for_boot(snapshot(), ["w:ABC"], Mock(), lambda: [])


@pytest.mark.parametrize("recovery_fails", [False, True])
def test_worker_runs_flash_then_aoa_before_ack_and_fence_release(tmp_path, recovery_fails):
    import hashlib
    from unittest.mock import MagicMock

    from worker_agent.app import WorkerAgent
    from worker_agent.config import WorkerConfig

    agent = WorkerAgent(WorkerConfig(worker_id="w", controller_url="https://controller",
                                    token="test", data_root=tmp_path / "worker", suite_roots=[]))
    agent.client = MagicMock()
    agent.runtime = Mock()
    image = b"firmware"
    agent.client.download.side_effect = lambda endpoint, target, **kw: target.write_bytes(image)
    calls = []
    command = {"id": "cmd", "command_type": "flash_firmware", "payload": {
        "stage_id": "fw-" + "b" * 32, "filename": "update.img", "size_bytes": len(image),
        "sha256": hashlib.sha256(image).hexdigest(), "devices": ["w:ABC"],
        "lease_tokens": [{"lease_id": "claim", "device_id": "w:ABC", "generation": 1, "attempt_id": "stage"}],
        "aoa_recovery": snapshot()}}

    def flash(*args, **kwargs):
        calls.append("flash")
        return {"success": True, "output": "upgrade ok"}

    def execute(*args, **kwargs):
        calls.append("aoa")
        agent.runtime.release_fencing.assert_not_called()
        if recovery_fails:
            raise RuntimeError("USB vanished")
        return {"success": True, "adb_recovered": True}

    with patch("worker_agent.app.flash_firmware", side_effect=flash), patch(
        "worker_agent.app.execute_device_action", side_effect=execute), patch(
        "worker_agent.app.probe_devices", return_value=[device()]), patch("worker_agent.app.CommandEventUploader"):
        agent.run_firmware_flash(command)
    assert calls == ["flash", "aoa"]
    args = agent.runtime.save_command.call_args.args
    assert args[1] == ("failed" if recovery_fails else "completed")
    assert args[2]["flash_success"] is True
    agent.runtime.release_fencing.assert_called_once_with(command)


def test_recover_after_flash_passes_gate_through_to_aoa():
    """恢复链路把门控原样透传给 aoa_sequence，供武装瞬间重查并发。"""
    captured = {}

    def execute(action, devices, options, **kwargs):
        captured.update(options)
        return {"success": True, "adb_recovered": True}

    def gate():
        return False

    result = recover_after_flash({"success": True, "output": "ok"}, snapshot(), ["w:ABC"],
                                 Mock(), execute,
                                 suppress_adb_daemon=True, suppress_adb_daemon_gate=gate)
    assert result["success"] is True
    assert captured["suppress_adb_daemon"] is True
    assert captured["suppress_adb_daemon_gate"] is gate
