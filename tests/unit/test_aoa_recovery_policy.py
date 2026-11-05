"""Unattended recovery binds opt-in evidence: no declaration means unverified replay."""
import copy

import pytest

from features.devices.aoa_calibrations import AoaCalibrationStore
from foundation.aoa_calibration import context_matches, convert_commands, mapping_options
from foundation.aoa_recovery_policy import (
    firmware_compatibility,
    require_firmware_digest,
    require_verified_recovery,
    target_firmware_context,
)
from foundation.error_model import ApiError
from tests.aoa_fixtures import firmware_declaration, verified_mapping


def valid_recovery():
    source, target = verified_mapping(), verified_mapping(1200, 1920)
    return ({"aoa_mapping_policy": "direct_calibrated", "aoa_touch_mapping": target.to_dict(),
             "aoa_source_touch_mapping": source.to_dict()},
            {"firmware_compatibility": firmware_declaration(target.context)})


@pytest.mark.parametrize("policy", ["estimated", "direct_unverified", "", "unsupported"])
def test_estimates_never_enter_unattended_touch_recovery(policy):
    options, metadata = valid_recovery()
    options["aoa_mapping_policy"] = policy
    with pytest.raises(ValueError, match="verified target mapping"):
        require_verified_recovery(["click 180 320"], options, metadata)


def test_missing_declaration_lets_unverified_recovery_proceed():
    """声明是选择性强化：未填写时放行，Worker 按未验证映射回放并如实标注。"""
    assert firmware_compatibility({}, ["click 180 320"]) is None
    require_verified_recovery(["click 180 320"], {"aoa_mapping_policy": "estimated"}, {})
    require_verified_recovery(["click 180 320"], {"aoa_mapping_policy": "direct_unverified"},
                              {"hid_touch_mode": "direct"})


@pytest.mark.parametrize("declaration", [{}, "measured", {"version": 2}, 7])
def test_malformed_firmware_declaration_is_rejected(declaration):
    with pytest.raises(ValueError, match="malformed"):
        firmware_compatibility({"firmware_compatibility": declaration}, ["click 180 320"])


def test_verified_cross_resolution_recovery_checks_declared_firmware():
    options, metadata = valid_recovery()
    require_verified_recovery(["click 180 320", "swipe 90 480 500 270 160"], options, metadata)
    require_firmware_digest(firmware_compatibility(metadata, ["click 180 320"]), "a" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        require_firmware_digest(metadata["firmware_compatibility"], "b" * 64)


@pytest.mark.parametrize("missing", ["version", "sha256", "context", "validated_by", "validated_at", "evidence"])
def test_incomplete_firmware_declaration_is_rejected(missing):
    _, metadata = valid_recovery()
    metadata["firmware_compatibility"].pop(missing)
    with pytest.raises(ValueError):
        firmware_compatibility(metadata, ["click 180 320"])


def test_declared_post_flash_context_is_distinct_from_pre_flash_inventory():
    mapping = verified_mapping()
    observed = copy.deepcopy(mapping.context)
    target = {**observed, "release": "17", "fingerprint": "tested-user-build"}
    metadata = {"firmware_compatibility": firmware_declaration(target)}
    actual = target_firmware_context(metadata, ["click 180 320"], observed)
    assert actual["fingerprint"] == "tested-user-build"
    assert observed["fingerprint"] == "build-a"
    with pytest.raises(ValueError, match="target device model"):
        target_firmware_context(metadata, ["click 180 320"], {**observed, "model": "another-board"})
    options, _ = valid_recovery()
    with pytest.raises(ValueError, match="target firmware environment"):
        require_verified_recovery(["click 180 320"], options, metadata)


def test_compatibility_receipt_is_copied_not_shared():
    _, metadata = valid_recovery()
    frozen = firmware_compatibility(metadata, ["click 180 320"])
    metadata["firmware_compatibility"]["context"]["fingerprint"] = "changed"
    assert frozen["context"]["fingerprint"] == "build-a"


@pytest.mark.parametrize("key", ["fingerprint", "display_route", "kernel", "density", "model", "release"])
@pytest.mark.parametrize("value", [None, "", "unknown"])
def test_both_unknown_environments_never_match_or_persist(tmp_path, key, value):
    mapping = verified_mapping()
    mapping.context[key] = value
    assert not context_matches(mapping.context, mapping.context)
    with pytest.raises(ApiError) as caught:
        AoaCalibrationStore(tmp_path).save_calibration("owner", mapping)
    assert caught.value.status_code == 422


@pytest.mark.parametrize("command", ["click 180 0", "swipe 180 320 500 180 0"])
def test_unreachable_target_is_rejected_instead_of_clamped(command):
    source, target = verified_mapping(), verified_mapping(offset_y=100)
    assert target.screen_to_hid(400, 0)[1] == 0  # Manual point inspection retains clamp.
    with pytest.raises(ValueError, match="reachable HID region"):
        convert_commands([command], source, target)


def test_plain_keyboard_recovery_does_not_require_touch_evidence():
    require_verified_recovery(["home", "key TAB ENTER"], {}, {})
    assert firmware_compatibility({}, ["home"]) is None


def test_standard_policy_cannot_be_claimed_with_a_firmware_declaration():
    options, _ = valid_recovery()
    with pytest.raises(ValueError, match="device compatibility"):
        mapping_options({**options, "aoa_hid_touch_mode": "direct", "aoa_mapping_policy": "direct_standard"})
