"""Physical topology, fencing, and independent cleanup without real USB."""
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from worker_agent import aoa_actions
from worker_agent.aoa_usb import bind_usb, find_bound_device


def device(serial="ABC", bus=1, ports=(2, 3), accessory=False):
    return SimpleNamespace(serial_number=serial, bus=bus, port_numbers=ports,
                           idVendor=0x18D1 if accessory else 0x2207,
                           idProduct=0x2D01 if accessory else 1)


def test_reenumeration_uses_port_even_when_accessory_serial_changes():
    original, accessory, other = device(), device("changed", accessory=True), device(bus=2)
    core = SimpleNamespace(find=lambda **_: [other, accessory])
    with bind_usb():
        assert find_bound_device(core, "ABC", lambda: original) is original
        assert find_bound_device(core, "ABC", lambda: other) is accessory


def test_same_serial_on_different_port_is_never_reused():
    core = SimpleNamespace(find=lambda **_: [device(bus=2)])
    with bind_usb("1-2.3"):
        assert find_bound_device(core, "ABC", lambda: device(bus=2)) is None


def test_replacement_native_device_on_same_port_is_rejected():
    core = SimpleNamespace(find=lambda **_: [device("replacement")])
    with bind_usb("1-2.3"), pytest.raises(RuntimeError, match="identity changed"):
        find_bound_device(core, "ABC", lambda: None)


def test_revoked_claim_blocks_rediscovery_and_hid(monkeypatch):
    check = Mock(side_effect=RuntimeError("stale claim"))
    transfer = Mock()
    with bind_usb("1-2.3", check):
        with pytest.raises(RuntimeError, match="stale claim"):
            find_bound_device(SimpleNamespace(), "ABC", lambda: None)
        with pytest.raises(RuntimeError, match="stale claim"):
            aoa_actions._transfer(SimpleNamespace(ctrl_transfer=transfer),
                                 aoa_actions.aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT, 1, 0)
    transfer.assert_not_called()


def test_calibration_adb_timeout_does_not_block_usb_cleanup(monkeypatch):
    target = SimpleNamespace(reset=Mock())
    dispose, native, adb_state = Mock(), Mock(return_value=True), Mock(return_value={})
    monkeypatch.setattr(aoa_actions, "_usb", lambda: (None, SimpleNamespace(dispose_resources=dispose)))
    monkeypatch.setattr(aoa_actions, "_configure_accessory", lambda *_: target)
    monkeypatch.setattr(aoa_actions, "_detach_kernel_drivers", lambda _: None)
    monkeypatch.setattr(aoa_actions, "_register_all_hid", lambda _: None)
    monkeypatch.setattr(aoa_actions, "_transfer", lambda *_: 0)
    monkeypatch.setattr(aoa_actions, "_ensure_native_mode", native)
    monkeypatch.setattr(aoa_actions, "_wait_adb_state", adb_state)

    def adb(serial, *args, **kwargs):
        if args[:3] == ("shell", "dumpsys", "input") or args[-1] == "4":
            raise subprocess.TimeoutExpired("adb", 1)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(aoa_actions, "_adb_run", adb)
    assert aoa_actions._calibrate_touch_mapping("ABC", 800, 1280, lambda _: None) is None
    target.reset.assert_called_once()
    dispose.assert_called_once_with(target)
    native.assert_called_once()
    adb_state.assert_called_once()


def test_adb_same_serial_on_wrong_port_does_not_count_as_recovered(monkeypatch):
    monkeypatch.setattr(aoa_actions.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout="List of devices attached\nABC device usb:2-4\n"))
    with bind_usb("1-2.3"):
        result = aoa_actions._wait_adb_state("ABC", 0)
    assert result["adb_recovered"] is False
    assert result["adb_state"] == "identity_mismatch"


@pytest.mark.parametrize("changed", ["display", "rotation", "model"])
def test_changed_display_invalidates_calibration_before_input(monkeypatch, changed):
    from worker_agent import aoa_calibration

    context = {"version": 2, "model": "model", "release": "16", "display": [800, 1280], "rotation": 0}
    current = {**context, changed: {"display": [1280, 800], "rotation": 1, "model": "other"}[changed]}
    monkeypatch.setattr(aoa_actions, "_display_size", lambda _: (800, 1280))
    monkeypatch.setattr(aoa_calibration, "display_context", lambda *_: current)
    adb = Mock()
    monkeypatch.setattr(aoa_actions, "_adb_run", adb)
    with pytest.raises(ValueError, match="configuration changed"):
        aoa_actions.record_aoa_input("ABC", {"action": "tap", "x": 100, "y": 100,
            "scale_x": 2.2, "scale_y": 2.2, "calibration_context": context})
    adb.assert_not_called()


def test_released_or_replaced_runtime_fence_blocks_more_input(tmp_path):
    from worker_agent.config import WorkerConfig
    from worker_agent.runtime import WorkerRuntime

    runtime = WorkerRuntime(WorkerConfig(worker_id="w", controller_url="http://127.0.0.1",
        token="synthetic", data_root=tmp_path))
    token = {"device_id": "w:ABC", "lease_id": "claim-1", "attempt_id": "op1", "generation": 1}
    command = {"command_type": "device_action", "payload": {"devices": ["w:ABC"], "lease_tokens": [token]}}
    runtime.validate_fencing(command)
    runtime.check_current_fencing(command)
    runtime.release_fencing(command)
    with pytest.raises(ValueError, match="fencing changed"):
        runtime.check_current_fencing(command)
    runtime.validate_fencing({**command, "payload": {"devices": ["w:ABC"], "lease_tokens": [
        {**token, "lease_id": "claim-2", "attempt_id": "op2", "generation": 2}]}})
    with pytest.raises(ValueError, match="fencing changed"):
        runtime.check_current_fencing(command)


@pytest.mark.parametrize("usb_request", [54, 55, 56, 57, 52, 53])
def test_revoked_claim_blocks_all_usb_mutations(usb_request):
    transfer = Mock()
    with bind_usb("1-2.3", Mock(side_effect=RuntimeError("stale claim"))), pytest.raises(RuntimeError, match="stale claim"):
        aoa_actions._transfer(SimpleNamespace(ctrl_transfer=transfer), usb_request, 1, 0)
    transfer.assert_not_called()


def test_failed_sequence_does_not_reset_usb_after_claim_revocation(monkeypatch):
    from worker_agent.aoa_usb import validate_binding

    revoked = False
    reset, dispose = Mock(), Mock()

    def validate():
        if revoked:
            raise RuntimeError("stale claim")

    def transfer(_type, request, value, index, data, **kwargs):
        nonlocal revoked
        if request == aoa_actions.aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT:
            revoked = True
            raise RuntimeError("input interrupted")
        return len(data) if isinstance(data, bytes) else 0

    target = SimpleNamespace(ctrl_transfer=transfer, reset=reset)
    monkeypatch.setattr(aoa_actions, "_usb", lambda: (None, SimpleNamespace(dispose_resources=dispose)))
    monkeypatch.setattr(aoa_actions, "_configure_accessory", lambda *_: target)
    monkeypatch.setattr(aoa_actions, "_detach_kernel_drivers", lambda _: None)
    monkeypatch.setattr(aoa_actions, "_ensure_native_mode", lambda *_: validate_binding())
    with bind_usb("1-2.3", validate), pytest.raises(aoa_actions.AoaError, match="input interrupted"):
        aoa_actions.run_aoa_sequence("ABC", ["click 10 20"], sleep=lambda _: None)
    reset.assert_not_called()
    dispose.assert_called_once_with(target)
