"""A started calibration Activity may still be covered or on another display."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.aoa_fixtures import verified_mapping
from worker_agent import aoa_actions
from worker_agent.aoa_calibration import open_calibration_surface, verify_calibration_surface


FOCUS = "mTopFocusedDisplayId=0\nmCurrentFocus=Window{123 u0 com.gms.remote.aoacalibration/.CalibrationActivity}\n"


@pytest.mark.parametrize("output", [
    "Status: ok", FOCUS.replace("DisplayId=0", "DisplayId=1"),
    FOCUS.replace("com.gms.remote.aoacalibration/.CalibrationActivity", "com.android.systemui/.Dialog"),
    FOCUS + "mKeyguardShowing=true", FOCUS + "mShowingLockscreen=true",
    FOCUS + "mCurrentFocus=Window{222 u0 android/.PermissionDialog}",
])
def test_focus_requires_the_expected_activity_display_and_unlocked_window(output):
    adb = Mock(return_value=SimpleNamespace(stdout=output, stderr="", returncode=0))
    with pytest.raises(ValueError, match="前台"):
        verify_calibration_surface("ABC", adb)


def test_launch_checks_focus_instead_of_trusting_status_ok():
    adb = Mock(side_effect=[SimpleNamespace(stdout="Status: ok", stderr="", returncode=0),
                           SimpleNamespace(stdout=FOCUS, stderr="", returncode=0)])
    open_calibration_surface("ABC", adb)
    assert adb.call_args.args[1:] == ("shell", "dumpsys", "window")
    assert "--display" in adb.call_args_list[0].args


def test_full_activity_component_is_accepted():
    adb = Mock(return_value=SimpleNamespace(stdout=FOCUS.replace(
        "/.CalibrationActivity", "/com.gms.remote.aoacalibration.CalibrationActivity"), stderr="", returncode=0))
    verify_calibration_surface("ABC", adb)


@pytest.mark.parametrize("lost_after_registration", [False, True])
def test_overlay_prevents_probe_input_and_does_not_reset_usb_before_setup(monkeypatch, lost_after_registration):
    transfers = []
    windows = [FOCUS, FOCUS.replace("/.CalibrationActivity", "/.CoveredActivity")]

    def adb(*args, **kwargs):
        if args[1:] == ("shell", "dumpsys", "window"):
            text = windows.pop(0) if lost_after_registration else "mCurrentFocus=null"
        else:
            text = "Status: ok"
        return SimpleNamespace(stdout=text, stderr="", returncode=0)

    configure, reset, restore, adb_state = Mock(return_value=object()), Mock(), Mock(return_value=True), Mock(return_value={"adb_recovered": True})
    monkeypatch.setattr(aoa_actions, "_usb", lambda: (None, SimpleNamespace(dispose_resources=Mock())))
    monkeypatch.setattr(aoa_actions, "_adb_run", adb)
    monkeypatch.setattr(aoa_actions, "_configure_accessory", configure)
    monkeypatch.setattr(aoa_actions, "_detach_kernel_drivers", Mock())
    monkeypatch.setattr(aoa_actions, "_register_all_hid", Mock())
    monkeypatch.setattr(aoa_actions, "_transfer", lambda *args: transfers.append(args) or 0)
    monkeypatch.setattr(aoa_actions, "_reset_usb", reset)
    monkeypatch.setattr(aoa_actions, "_ensure_native_mode", restore)
    monkeypatch.setattr(aoa_actions, "_wait_adb_state", adb_state)
    assert aoa_actions._calibrate_touch_affine("ABC", 800, 1280, lambda _: None,
        "direct", context=verified_mapping().context) is None
    assert not any(args[1] == 57 and args[-1][0] == 3 for args in transfers)
    assert configure.call_count == int(lost_after_registration)
    assert reset.call_count == int(lost_after_registration)
    assert restore.call_count == int(lost_after_registration)
    assert adb_state.call_count == int(lost_after_registration)
