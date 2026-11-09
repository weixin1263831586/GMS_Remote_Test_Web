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

    commands = ["home", "sleep 2000"]
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


def test_post_flash_mapping_reaches_execution_without_adb_metadata_queries():
    from tests.aoa_fixtures import verified_mapping

    mapping = verified_mapping().to_dict()
    frozen = snapshot()
    frozen["metadata"] = {"hid_touch_mode": "direct"}
    frozen["mapping_options"] = {"aoa_touch_mapping": mapping, "aoa_source_touch_mapping": mapping,
                                 "aoa_mapping_policy": "direct_calibrated"}
    execute = Mock(return_value={"success": True, "adb_recovered": True})
    result = recover_after_flash({"success": True}, frozen, ["w:ABC"], Mock(), execute)
    assert result["stage"] == "ADB_READY"
    options = execute.call_args.args[2]
    assert options["aoa_touch_mapping"] == mapping
    assert options["aoa_source_touch_mapping"] == mapping


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


def test_interrupted_recovery_keeps_failed_step_and_authorization_state():
    error = RuntimeError("AOA step 63 failed; ADB unauthorized")
    error.aoa_result = {"success": False, "failed_index": 63, "commands_executed": 62,
                        "adb_state": "unauthorized", "adb_recovered": False}
    execute = Mock(side_effect=error)
    result = recover_after_flash({"success": True}, snapshot(), ["w:ABC"], Mock(), execute)
    assert result["flash_success"] is True and result["success"] is False
    assert result["aoa_recovery"] == error.aoa_result
    assert result["stage"] == "AOA_RECOVERY_FAILED"
    execute.assert_called_once()


def test_completed_input_retains_adb_failure_details():
    recovered = {"success": False, "adb_recovered": False, "adb_state": "unauthorized",
                 "adb_hint": "请确认电脑 RSA 授权弹框"}
    result = recover_after_flash({"success": True}, snapshot(), ["w:ABC"], Mock(),
                                  Mock(return_value=recovered))
    assert result["aoa_recovery"] == recovered
    assert "RSA" in result["error"] and "无需重新烧写" in result["error"]
    assert result["stage"] == "AOA_RECOVERY_FAILED"


def test_replay_done_with_dead_adbd_is_not_reported_as_input_failure():
    """69 步输入已全部执行、仅 ADB 未恢复时：如实区分，不再笼统报恢复失败。"""
    recovered = {"success": False, "adb_recovered": False, "replay_completed": True,
                 "adb_state": "io_dead",
                 "adb_hint": "ADB 显示 device 但 I/O 无响应（adbd 半死）；请重启设备后再验证"}
    result = recover_after_flash({"success": True}, snapshot(), ["w:ABC"], Mock(),
                                  Mock(return_value=recovered))
    assert result["success"] is False and result["flash_success"] is True
    assert result["stage"] == "REPLAY_DONE_ADB_UNAVAILABLE"
    assert result["replay_completed"] is True
    assert "回放均已完成" in result["error"] and "无需重新烧写或回放" in result["error"]
    assert "io_dead" in result["error"] or "adbd 半死" in result["error"]


def test_connected_adb_with_failed_usb_restore_is_not_reported_as_missing_adb():
    recovered = {"success": False, "adb_recovered": True, "adb_state": "device", "native_mode_restored": False}
    execute = Mock(return_value=recovered)
    result = recover_after_flash({"success": True}, snapshot(), ["w:ABC"], Mock(), execute)
    assert result["success"] is False and result["flash_success"] is True
    assert "ADB 已连接" in result["error"] and "USB" in result["error"]
    assert "尚未恢复或尚未授权" not in result["error"]
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


def test_touch_recovery_requires_bound_firmware_before_flash():
    import hashlib
    import json

    from tests.aoa_fixtures import firmware_declaration, verified_mapping

    frozen = snapshot()
    mapping = verified_mapping().to_dict()
    frozen["commands"] = ["click 180 320"]
    frozen["commands_sha256"] = hashlib.sha256(json.dumps(frozen["commands"], separators=(",", ":")).encode()).hexdigest()
    declaration = firmware_declaration()
    frozen.update({"metadata": {"hid_touch_mode": "direct", "firmware_compatibility": declaration},
                   "firmware_compatibility": declaration,
                   "mapping_options": {"aoa_touch_mapping": mapping, "aoa_source_touch_mapping": mapping,
                                       "aoa_mapping_policy": "direct_calibrated"}})
    validate_preflash(frozen, ["w:ABC"], Mock(), lambda: [device()], firmware_sha256="a" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        validate_preflash(frozen, ["w:ABC"], Mock(), lambda: [device()], firmware_sha256="b" * 64)
    frozen["metadata"] = {"hid_touch_mode": "direct"}
    # 声明已是可选强化；但已冻结的声明不得在冻结后被移除（篡改检测仍在）。
    with pytest.raises(ValueError, match="snapshot changed"):
        recovery_options(frozen)


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
        RuntimeError, match="未等到原 USB 端口的 Android 设备"):
        wait_for_boot(snapshot(), ["w:ABC"], Mock(), lambda: [])


def test_boot_wait_kicks_half_dead_adbd_and_reports_blocker():
    """半死态放行但趁 settle 窗口踢腿：settle（60s）内首踢，settle 满即回放。"""
    kick = Mock()
    events = []
    snap = snapshot()
    snap["boot_wait_seconds"] = 60
    clock = iter([0, 0, 0, 30, 30, 30, 31, 31, 31, 60, 60, 60])
    with patch("worker_agent.firmware_recovery.time.monotonic",
               side_effect=lambda: next(clock)), patch(
        "worker_agent.firmware_recovery.time.sleep"):
        wait_for_boot(snap, ["w:ABC"], Mock(),
                      lambda: [device(state="unresponsive")], events.append, kick=kick)
    assert kick.call_count == 1
    commands = [event["command"] for event in events]
    assert any("adb reconnect（第 1/3 次）" in command for command in commands)
    assert any("回放不依赖 adbd" in command for command in commands)


def test_boot_wait_passes_half_dead_adbd_without_kick():
    """无踢腿能力（如 adb daemon 被抑制）时半死态也放行：不抛未就绪超时。"""
    probe = Mock(side_effect=lambda: [device(state="unresponsive")])
    with patch("worker_agent.firmware_recovery.time.sleep"):
        wait_for_boot(snapshot(), ["w:ABC"], Mock(), probe, None, kick=None)
    probe.assert_called_once()


def test_boot_wait_intercepts_ghost_adb_entry_on_other_port():
    """同 serial 的 adb 缓存幽灵条目不在冻结端口上：拦截，绝不能回放到别的设备。"""
    ghost = device(port="1-7", state="device")
    with pytest.raises(RuntimeError, match="identity changed"):
        wait_for_boot(snapshot(), ["w:ABC"], Mock(), lambda: [ghost])


def test_boot_wait_reports_failed_kick_dispatch():
    """adb server 不可用导致 reconnect 下发失败时，踢腿事件必须可见。"""
    kick = Mock(return_value=False)
    events = []
    snap = snapshot()
    snap["boot_wait_seconds"] = 60
    clock = iter([0, 0, 0, 30, 30, 30, 31, 31, 31, 60, 60, 60])
    with patch("worker_agent.firmware_recovery.time.monotonic",
               side_effect=lambda: next(clock)), patch(
        "worker_agent.firmware_recovery.time.sleep"):
        wait_for_boot(snap, ["w:ABC"], Mock(),
                      lambda: [device(state="unresponsive")], events.append, kick=kick)
    assert any("下发失败" in event["command"] for event in events)


def test_boot_wait_does_not_kick_when_device_absent():
    """设备未回原端口时不做 adb reconnect（踢腿只针对 adbd 半死态）。"""
    kick = Mock()
    with patch("worker_agent.firmware_recovery.time.monotonic", side_effect=[0, 181]), pytest.raises(
        RuntimeError, match="未等到原 USB 端口的 Android 设备"):
        wait_for_boot(snapshot(), ["w:ABC"], Mock(), lambda: [], None, kick=kick)
    kick.assert_not_called()


def test_boot_wait_reports_situation_transitions_immediately():
    """状态迁移即时上报：出现/消失/半死即刻成行，缺席不打 0 秒噪音。"""
    events = []
    states = iter([[], [device(state="unresponsive")], [],
                   [device(state="unresponsive")], [device(state="unresponsive")]])
    probe = Mock(side_effect=lambda: next(states))
    snap = snapshot()
    snap["boot_wait_seconds"] = 60
    clock = iter([0, 0, 5, 10, 20, 90])
    with patch("worker_agent.firmware_recovery.time.monotonic",
               side_effect=lambda: next(clock)), patch(
        "worker_agent.firmware_recovery.time.sleep"):
        wait_for_boot(snap, ["w:ABC"], Mock(), probe, events.append, kick=Mock())
    commands = [event["command"] for event in events]
    assert not any("已等待 0 秒" in command for command in commands)
    assert any("消失（重枚举中）" in command for command in commands)
    assert sum("adbd 半死" in command for command in commands) == 2


def test_boot_wait_resumes_when_adbd_becomes_ready():
    """半死态自行恢复且未浪费踢腿：settle 从首次就绪起算，满 60s 回放。"""
    states = iter(["unresponsive", "device", "device"])
    probe = Mock(side_effect=lambda: [device(state=next(states))])
    kick = Mock()
    snap = snapshot()
    snap["boot_wait_seconds"] = 60
    clock = iter([0, 0, 31, 62])
    with patch("worker_agent.firmware_recovery.time.monotonic",
               side_effect=lambda: next(clock)), patch(
        "worker_agent.firmware_recovery.time.sleep"):
        wait_for_boot(snap, ["w:ABC"], Mock(), probe, None, kick=kick)
    assert probe.call_count == 3
    kick.assert_not_called()


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
    agent.heartbeat = Mock()  # 恢复后的终态清单强制上报不真发心跳
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
    agent.heartbeat.assert_called_once()  # 终态清单随命令收尾强制上报
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
