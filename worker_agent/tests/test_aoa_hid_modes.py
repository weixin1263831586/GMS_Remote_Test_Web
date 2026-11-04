"""Legacy pointer coordinates and opt-in direct touch must remain independent."""
import pytest

from foundation import aoa_profile
from foundation.aoa_hid_mode import POINTER_TOUCH_DESCRIPTOR, hid_registrations, profile_hid_options
from worker_agent import aoa_actions

from .test_aoa_actions import _stall_usb


def test_old_profiles_default_to_pointer_and_version_two_calibration_stays_direct():
    assert profile_hid_options({}) == {"aoa_hid_touch_mode": "pointer", "aoa_hid_settle_seconds": 2,
                                       "aoa_hid_session_mode": "continuous", "aoa_mapping_policy": ""}
    assert profile_hid_options({"calibration": {"context": {"version": 2}}})["aoa_hid_touch_mode"] == "direct"
    assert profile_hid_options({"hid_touch_mode": "pointer", "calibration": {"context": {"version": 2}}})["aoa_hid_touch_mode"] == "pointer"


@pytest.mark.parametrize("metadata", [None, {}, {"calibration": None}, {"calibration": {}},
    {"calibration": {"context": None}}, {"calibration": {"context": {}}},
    {"calibration": {"context": {"version": None}}},
    {"calibration": {"context": {"version": 1}}}])
def test_unversioned_calibration_preserves_legacy_pointer_mode(metadata):
    assert profile_hid_options(metadata)["aoa_hid_touch_mode"] == "pointer"


@pytest.mark.parametrize("version", [None, 0, 1, 2, 3])
@pytest.mark.parametrize("mode", ["pointer", "direct"])
def test_explicit_mode_does_not_require_calibration_version(version, mode):
    metadata = {"hid_touch_mode": mode, "calibration": {"context": {"version": version}}}
    assert profile_hid_options(metadata)["aoa_hid_touch_mode"] == mode


@pytest.mark.parametrize("version", ["2", 2.0, True, -1, [], {}])
def test_invalid_calibration_version_is_a_validation_error(version):
    with pytest.raises(ValueError, match="context version"):
        profile_hid_options({"calibration": {"context": {"version": version}}})


@pytest.mark.parametrize("version", [2, 3])
def test_versioned_direct_calibration_keeps_its_mode(version):
    assert profile_hid_options({"calibration": {"context": {"version": version}}})["aoa_hid_touch_mode"] == "direct"


@pytest.mark.parametrize("metadata", [
    {"hid_touch_mode": "auto"}, {"hid_settle_seconds": -1}, {"hid_settle_seconds": 11},
    {"hid_settle_seconds": True}, {"hid_settle_seconds": float("nan")}, {"calibration": []},
])
def test_invalid_mode_or_wait_is_rejected(metadata):
    with pytest.raises(ValueError):
        profile_hid_options(metadata)


@pytest.mark.parametrize("mode", ["pointer", "direct"])
def test_descriptor_and_525_coordinate_are_preserved_after_input_device_settles(monkeypatch, mode):
    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, set())
    transfer = aoa_actions._transfer
    timeline = []

    def record(device, request, value=0, index=0, data=b""):
        if request == aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC:
            timeline.append(("descriptor", value, bytes(data)))
        if request == aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT:
            timeline.append(("input", value, bytes(data)))
        return transfer(device, request, value, index, data)

    monkeypatch.setattr(aoa_actions, "_transfer", record)
    result = aoa_actions.run_aoa_sequence("partial-hid-device", ["click 180 525", "back"],
        touch_mode=mode, hid_settle_seconds=3, sleep=lambda seconds: timeline.append(("sleep", seconds)))
    descriptors = [event for event in timeline if event[0] == "descriptor"]
    touch = next(event[2] for event in descriptors if event[1] == 3)
    assert touch == (POINTER_TOUCH_DESCRIPTOR if mode == "pointer" else aoa_profile.TOUCH_SCREEN_HID_DESCRIPTOR)
    first_input = next(index for index, event in enumerate(timeline) if event[0] == "input")
    assert timeline[first_input][2] == aoa_profile.touch_report(True, 180, 525)
    last_descriptor = max(index for index, event in enumerate(timeline) if event[0] == "descriptor")
    assert ("sleep", 3) in timeline[last_descriptor + 1:first_input]
    assert [event[1] for event in descriptors] == [3, 2]
    assert result["hid_touch_mode"] == mode
    assert result["commands_executed"] == 2


def test_modes_have_distinct_descriptors_and_the_same_hid_ids():
    pointer = dict(hid_registrations("pointer"))
    direct = dict(hid_registrations("direct"))
    assert pointer.keys() == direct.keys()
    assert pointer[3] != direct[3] and pointer[1] == direct[1] and pointer[2] == direct[2]
    assert b"\xa1\x00" in pointer[3] and b"\x09\x51" not in pointer[3]


def test_cross_mode_touch_calibration_is_rejected():
    with pytest.raises(ValueError, match="another HID mode"):
        aoa_profile.parse_record_input_action({"action": "tap", "x": 10, "y": 10,
            "hid_touch_mode": "pointer", "scale_x": 3.3, "scale_y": 3,
            "calibration_context": {"version": aoa_profile.AOA_TOUCH_CALIBRATION_VERSION,
                                    "model": "RK", "display": [1200, 1920],
                                    "rotation": 0, "hid_touch_mode": "direct"}})
