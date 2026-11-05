"""Check actual outgoing HID coordinates in continuous, stepped and debug replay."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from foundation.aoa_calibration import TouchMapping
from worker_agent import aoa_actions
from worker_agent.tests.test_aoa_actions import _stall_device_factory, _stall_usb


@pytest.mark.parametrize("session_mode", ["continuous", "per_command", "debug"])
def test_outgoing_coordinates_are_transformed_exactly_once(monkeypatch, session_mode):
    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, set())
    core, _ = aoa_actions._usb()
    monkeypatch.setattr(aoa_actions, "_usb", lambda: (core, SimpleNamespace(dispose_resources=lambda _: None)))
    monkeypatch.setattr(aoa_actions, "_configure_accessory", lambda *a, **kw: _stall_device_factory(state, set()))
    monkeypatch.setattr(aoa_actions, "_ensure_native_mode", lambda *a: True)
    clock = [0.0]
    monkeypatch.setattr(aoa_actions.time, "monotonic", lambda: clock[0])
    source = TouchMapping("direct", (2, 0, 0, 2), (0, 0), mapping_policy="direct_unverified", display_pixels=(800, 1280))
    target = TouchMapping("direct", (4, 0, 0, 4), (0, 0), mapping_policy="direct_unverified", display_pixels=(800, 1280))
    sent = [False]
    def queue(**kwargs):
        if not sent[0]:
            sent[0] = True
            return {"sequence": 1, "command": "click 180 320"}
        return None
    result = aoa_actions.run_aoa_sequence("partial-hid-device", ["click 180 320"],
        touch_mode="direct", session_mode="continuous" if session_mode == "debug" else session_mode,
        source_touch_mapping=source.to_dict(), touch_mapping=target.to_dict(),
        debug_command_source=queue if session_mode == "debug" else None,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert result["commands_executed"] == 1
    downs = [data for channel, data in state["events"] if channel == 3 and data[0] == 3]
    assert downs == [aoa_actions.aoa_profile.touch_report(True, 90, 160)]


def test_unsupported_mapping_never_enters_usb_for_touch(monkeypatch):
    usb = Mock(side_effect=AssertionError("USB must not be opened"))
    monkeypatch.setattr(aoa_actions, "_usb", usb)
    target = TouchMapping("direct", (2, 0, 0, 2), (0, 0), mapping_policy="unsupported", display_pixels=(800, 1280))
    with pytest.raises(aoa_actions.AoaError, match="forbids"):
        aoa_actions.run_aoa_sequence("ABC", ["click 180 320"], touch_mode="direct",
            touch_mapping=target.to_dict(), mapping_policy="unsupported")
    usb.assert_not_called()


def test_failed_probes_never_claim_success_or_measurement(monkeypatch):
    from tests.aoa_fixtures import verified_mapping

    monkeypatch.setattr(aoa_actions, "_display_size", lambda _: (800, 1280))
    monkeypatch.setattr("worker_agent.aoa_calibration.display_context", lambda *a: verified_mapping().context)
    monkeypatch.setattr(aoa_actions, "_calibrate_touch_affine", lambda *a, **kw: None)
    monkeypatch.setattr(aoa_actions, "cleanup_diagnostics", lambda: [])
    result = aoa_actions.record_aoa_input("ABC", {"action": "calibrate", "hid_touch_mode": "direct"})
    assert result["success"] is False
    assert result["touch_calibration"]["measured"] is False
    assert result["touch_calibration"]["verified"] is False


def test_safety_activity_required_before_usb_registration(monkeypatch):
    from worker_agent.aoa_calibration import open_calibration_surface

    adb = Mock(return_value=SimpleNamespace(returncode=0, stdout="Error type 3", stderr=""))
    with pytest.raises(ValueError, match="安全标定应用"):
        open_calibration_surface("ABC", adb)


def test_target_inventory_contains_real_display_and_firmware_evidence():
    from worker_agent.aoa_calibration import inventory_context

    context = inventory_context("""__FINGERPRINT__
build-a
__WM_SIZE__
Physical size: 800x1280
Override size: 1200x1920
__WM_DENSITY__
Physical density: 160
Override density: 240
__KERNEL__
test-kernel
__INPUT__
SurfaceOrientation: 0
uniqueId='local:0'
""", {"model": "RK3576", "android_version": "16"})
    assert context["display"] == [1200, 1920]
    assert context["density"] == 240
    assert context["fingerprint"] == "build-a"
    assert context["display_route"] == "local:0"


@pytest.mark.parametrize("bad_holdout", [False, True])
def test_real_calibration_pipeline_separates_fitting_and_validation(monkeypatch, bad_holdout):
    from tests.aoa_fixtures import verified_mapping

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (None, SimpleNamespace(dispose_resources=lambda _: None)))
    monkeypatch.setattr(aoa_actions, "_adb_run", lambda *a, **kw: SimpleNamespace(
        stdout="Status: ok\nmTopFocusedDisplayId=0\nmCurrentFocus=Window{123 u0 com.gms.remote.aoacalibration/.CalibrationActivity}",
        stderr="", returncode=0))
    monkeypatch.setattr(aoa_actions, "_configure_accessory", lambda *a: object())
    monkeypatch.setattr(aoa_actions, "_detach_kernel_drivers", lambda _: None)
    monkeypatch.setattr(aoa_actions, "_register_all_hid", lambda *a: None)
    monkeypatch.setattr(aoa_actions, "_transfer", lambda *a: 0)
    monkeypatch.setattr(aoa_actions, "_reset_usb", lambda _: None)
    monkeypatch.setattr(aoa_actions, "_ensure_native_mode", lambda *a: True)
    monkeypatch.setattr(aoa_actions, "_wait_adb_state", lambda *a: {"adb_recovered": True})
    monkeypatch.setattr(aoa_actions, "_parse_probe_cooked_point", lambda dump, x, y:
        (x * 800 / 361 + (20 if bad_holdout and x == 120 else 0), y * 1280 / 641))
    mapping = aoa_actions._calibrate_touch_affine("ABC", 800, 1280, lambda _: None,
        "direct", context=verified_mapping().context)
    assert mapping is not None
    assert len(mapping.samples) == 7
    assert sum(s["validation"] for s in mapping.samples) == 2
    assert mapping.verified is not bad_holdout
