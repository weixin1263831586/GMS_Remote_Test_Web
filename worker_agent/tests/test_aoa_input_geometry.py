"""Recorder clicks must address the native display, independently of video size."""
from types import SimpleNamespace

import pytest

from foundation.aoa_profile import parse_record_input_action
from worker_agent import aoa_actions


def recorder(monkeypatch, size="Physical size: 1440x2560", dump="SurfaceOrientation: 0", display_dump="",
             input_code=0, display_code=0):
    calls = []

    def adb(_serial, *args, **_kwargs):
        calls.append(args)
        output = size if args == ("shell", "wm", "size") else ""
        code = 0
        if args == ("shell", "dumpsys", "input"):
            output, code = dump, input_code
        if args == ("shell", "dumpsys", "display"):
            output, code = display_dump, display_code
        return SimpleNamespace(returncode=code, stdout=output, stderr="")

    monkeypatch.setattr(aoa_actions, "_adb_run", adb)
    return calls


@pytest.mark.parametrize(("action", "expected", "command"), [
    ({"action": "tap", "x": 320, "y": 864},
     ("tap", "800", "2160"), "click 200 540"),
    ({"action": "long_click", "x": 320, "y": 864, "duration_ms": 900},
     ("swipe", "800", "2160", "800", "2160", "900"), "longClick 200 540"),
    ({"action": "swipe", "x1": 320, "y1": 864, "x2": 288, "y2": 256, "duration_ms": 400},
     ("swipe", "800", "2160", "720", "640", "400"), "swipe 200 540 400 180 160"),
])
def test_downscaled_video_uses_native_pixels_for_adb_and_recording(monkeypatch, action, expected, command):
    calls = recorder(monkeypatch)
    result = aoa_actions.record_aoa_input("REC", {**action, "frame_size": [576, 1024]})
    assert ("shell", "input", *expected) in calls
    assert result["command"] == command
    assert result["display"] == [1440, 2560]


@pytest.mark.parametrize("rotation", [1, 3])
def test_landscape_fallback_rotates_wm_size(monkeypatch, rotation):
    calls = recorder(monkeypatch, dump=f"SurfaceOrientation: {rotation}")
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 864, "y": 320, "frame_size": [1024, 576]})
    assert result["display"] == [2560, 1440]
    assert ("shell", "input", "tap", "2160", "800") in calls


def test_current_logical_viewport_wins_over_physical_wm_size(monkeypatch):
    calls = recorder(monkeypatch, dump=(
        "SurfaceOrientation: 1\n"
        "Viewport INTERNAL: displayId=0, uniqueId='local:0', orientation=1, "
        "logicalFrame=[0, 0, 1280, 800]\n"))
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 512, "y": 320, "frame_size": [1024, 640]})
    assert result["display"] == [1280, 800]
    assert ("shell", "input", "tap", "640", "400") in calls
    assert result["command"] == "click 180 320"


@pytest.mark.parametrize("dump", [
    "Viewport INTERNAL: displayId=0, uniqueId=local:0, orientation=0, logicalFrame=[0, 0, 1200, 1920]",
    "mViewports=[DisplayViewport{type=INTERNAL, valid=true, isActive=true, displayId=0, "
    "uniqueId='local:0', orientation=0, logicalFrame=Rect(0, 0 - 1200, 1920)}]",
])
def test_android_viewport_formats_record_in_native_pixels(monkeypatch, dump):
    calls = recorder(monkeypatch, size="Physical size: 1200x1920", dump=dump)
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 320, "y": 864, "frame_size": [640, 1024]})
    assert result["display"] == [1200, 1920]
    assert ("shell", "input", "tap", "600", "1620") in calls
    assert result["command"] == "click 180 540"


@pytest.mark.parametrize("input_code", [0, 1])
def test_display_manager_fallback_verifies_primary_instead_of_guessing(monkeypatch, input_code):
    dump = ("mViewports=[DisplayViewport{type=INTERNAL, valid=true, isActive=true, displayId=0, "
            "uniqueId='local:0', orientation=0, logicalFrame=Rect(0, 0 - 1200, 1920)}]")
    calls = recorder(monkeypatch, size="Physical size: 1200x1920", dump="Input Reader State:\n",
                     display_dump=dump, input_code=input_code)
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 320, "y": 864, "frame_size": [640, 1024]})
    assert result["display"] == [1200, 1920]
    assert ("shell", "input", "tap", "600", "1620") in calls


def test_failed_display_manager_output_cannot_establish_geometry(monkeypatch):
    dump = ("DisplayViewport{displayId=0, uniqueId='local:0', orientation=0, "
            "logicalFrame=Rect(0, 0 - 1200, 1920)}")
    calls = recorder(monkeypatch, dump="", display_dump=dump, display_code=1)
    with pytest.raises(ValueError, match="主显示器"):
        aoa_actions.record_aoa_input("REC", {
            "action": "tap", "x": 320, "y": 864, "frame_size": [640, 1024]})
    assert not any(call[:2] == ("shell", "input") for call in calls)


def test_native_screenshot_and_override_size(monkeypatch):
    calls = recorder(monkeypatch, size="Physical size: 1440x2560\nOverride size: 720x1280")
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 400, "y": 1080, "frame_size": [720, 1280]})
    assert result["display"] == [720, 1280]
    assert ("shell", "input", "tap", "400", "1080") in calls
    assert result["command"] == "click 200 540"


def test_encoder_rounding_is_allowed_and_last_pixel_is_clamped(monkeypatch):
    calls = recorder(monkeypatch, size="Physical size: 720x1280")
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 571, "y": 1023, "frame_size": [572, 1024]})
    assert result["display"] == [720, 1280]
    assert ("shell", "input", "tap", "719", "1279") in calls


def test_video_conversion_precedes_measured_hid_mapping(monkeypatch):
    from tests.aoa_fixtures import verified_mapping

    mapping = verified_mapping(1440, 2560, mode="pointer", offset_y=40)
    calls = recorder(monkeypatch)
    monkeypatch.setattr("worker_agent.aoa_calibration.display_context", lambda *_: mapping.context)
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 320, "y": 864, "frame_size": [576, 1024],
        "touch_mapping": mapping.to_dict()})
    assert ("shell", "input", "tap", "800", "2160") in calls
    assert result["command"] == "click 201 531"


@pytest.mark.parametrize("dump", [
    "SurfaceOrientation: 1", "", "SurfaceOrientation: 0\nSurfaceOrientation: 1",
    "SurfaceOrientation: 0\nViewport INTERNAL: displayId=1",
])
def test_stale_or_unverifiable_display_never_sends_input(monkeypatch, dump):
    calls = recorder(monkeypatch, dump=dump)
    with pytest.raises(ValueError, match="投屏"):
        aoa_actions.record_aoa_input("REC", {
            "action": "tap", "x": 320, "y": 864, "frame_size": [576, 1024]})
    assert not any(call[:2] == ("shell", "input") for call in calls)


@pytest.mark.parametrize("size", [None, [], [576], [576, 1024, 1], [True, 1024],
                                 [576.5, 1024], [0, 1024], [576, 10001]])
def test_invalid_frame_size_rejected(size):
    with pytest.raises(ValueError, match="frame_size"):
        parse_record_input_action({"action": "tap", "x": 2, "y": 3, "frame_size": size})


@pytest.mark.parametrize("action", [
    {"action": "tap", "x": 576, "y": 10},
    {"action": "long_click", "x": 10, "y": 1024},
    {"action": "swipe", "x1": 10, "y1": 20, "x2": 576, "y2": 30},
    {"action": "home"},
])
def test_frame_coordinates_must_be_in_bounds_for_pointer_actions(action):
    with pytest.raises(ValueError, match="frame_size"):
        parse_record_input_action({**action, "frame_size": [576, 1024]})


@pytest.mark.parametrize("dump", [
    # Android 17 InputFlinger dump：SurfaceOrientation 行已移除，
    # 改为 reader state 内的 InputDeviceOrientation: RotationN。
    "InputDeviceOrientation: Rotation0\nViewport INTERNAL: displayId=0, "
    "uniqueId=local:0, orientation=0, logicalFrame=[0, 0, 1080, 1920]",
    "Viewport INTERNAL: displayId=0, uniqueId=local:0, orientation=0, "
    "logicalFrame=[0, 0, 1080, 1920]\nInputDeviceOrientation: Rotation1",
])
def test_android17_orientation_tokens_are_recognized(monkeypatch, dump):
    calls = recorder(monkeypatch, size="Physical size: 1080x1920", dump=dump)
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 540, "y": 960, "frame_size": [1080, 1920]})
    assert result["display"] == [1080, 1920]
    assert ("shell", "input", "tap", "540", "960") in calls


def test_android17_landscape_rotation_rotates_display(monkeypatch):
    calls = recorder(monkeypatch, size="Physical size: 1080x1920", dump=(
        "InputDeviceOrientation: Rotation3\n"
        "Viewport INTERNAL: displayId=0, uniqueId=local:0, orientation=3, "
        "logicalFrame=[0, 0, 1920, 1080]\n"))
    result = aoa_actions.record_aoa_input("REC", {
        "action": "tap", "x": 960, "y": 540, "frame_size": [1920, 1080]})
    assert result["display"] == [1920, 1080]
    assert ("shell", "input", "tap", "960", "540") in calls
