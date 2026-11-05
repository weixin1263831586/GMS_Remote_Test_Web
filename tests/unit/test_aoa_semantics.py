"""Certification cannot be replaced by flags, fitted offsets or untested displays."""
import copy

import pytest

from foundation.aoa_calibration import mapping_options
from foundation.aoa_certification import (
    build_certificate,
    matching_case,
    validate_certificate,
    validate_reference_evidence,
)
from foundation.aoa_display import input_displays, standard_inverse, standard_point, target_point
from foundation.aoa_semantics import compile_plan, validate_plan
from tests.aoa_semantic_fixtures import certificate, reference, semantic_template


@pytest.mark.parametrize("rotation", range(4))
def test_standard_transform_is_invertible_in_explicit_viewport(rotation):
    for point in [(40, 80), (180, 320), (320, 560)]:
        cooked = standard_point(*point, [1200, 1800], rotation, [100, 200, 1100, 1600])
        assert standard_inverse(*cooked, [1200, 1800], rotation, [100, 200, 1100, 1600]) == point
    assert validate_reference_evidence(reference(rotation=rotation))["axis_rotation"] == rotation


@pytest.mark.parametrize("mutation", ["pointer", "axis", "descriptor", "offset", "display", "duplicate", "wrong-node"])
def test_certification_rejects_unproved_or_nonstandard_input(mutation):
    evidence = reference()
    if mutation == "pointer":
        evidence["getevent_after"] = evidence["getevent_after"].replace("INPUT_PROP_DIRECT", "INPUT_PROP_POINTER")
    elif mutation == "axis":
        evidence["getevent_after"] = evidence["getevent_after"].replace("max 640", "max 639")
    elif mutation == "descriptor":
        evidence["descriptor_sha256"] = "0" * 64
    elif mutation == "offset":
        for sample in evidence["samples"]:
            sample["cooked"][1] += 20
    elif mutation == "display":
        evidence["context"]["display_route"] = "another-display"
    elif mutation == "duplicate":
        evidence["samples"][-1] = evidence["samples"][0]
    else:
        evidence["input_reader"] += evidence["input_reader"].replace("Device 9:", "Device 10:")
    with pytest.raises(ValueError):
        validate_reference_evidence(evidence)


def test_distinct_reference_devices_and_immutable_evidence_required():
    with pytest.raises(ValueError, match="distinct"):
        build_certificate([reference(), reference()], certificate_id="id", owner_id="owner", label="test")
    proof = certificate()
    proof["cases"][0]["axis_rotation"] = 1
    with pytest.raises(ValueError, match="does not match"):
        validate_certificate(proof)


@pytest.mark.parametrize("key,value", [("rotation", 1), ("display", [1920, 1080]), ("fingerprint", "new-firmware"), ("display_id", 1)])
def test_unknown_display_or_firmware_does_not_reuse_certification(key, value):
    context = {**reference()["context"], key: value}
    assert matching_case(certificate(), context) is None


def test_semantic_plan_rejects_tampering_and_preserves_standard_policy():
    context, proof = reference()["context"], certificate()
    template = semantic_template()
    template["steps"][0]["before"] = {"type": "visual", "roi": [0, 0, 1, 1],
        "binding_id": "camera-1", "fingerprint": "00" * 256}
    plan = compile_plan(["click 1 1"], template, context, proof)
    assert plan["commands"] == ["click 180 481"]
    assert mapping_options({"aoa_hid_touch_mode": "direct", "aoa_semantic_plan": plan,
                           "aoa_direct_certificate": proof})["aoa_mapping_policy"] == "direct_standard"
    plan["commands"][0] = "click 180 1"
    with pytest.raises(ValueError):
        validate_plan(plan, proof)
    with pytest.raises(ValueError):
        mapping_options({"aoa_hid_touch_mode": "direct", "aoa_mapping_policy": "direct_standard"})


def test_ambiguous_layouts_language_mismatch_and_blind_preconditions_rejected():
    context, proof = reference()["context"], certificate()
    template = semantic_template()
    template["layouts"].append({**copy.deepcopy(template["layouts"][0]), "id": "duplicate-layout"})
    with pytest.raises(ValueError, match="ambiguous"):
        compile_plan(["click 1 1"], template, context, proof)
    with pytest.raises(ValueError, match="unknown"):
        compile_plan(["click 1 1"], semantic_template(), {**context, "locale": "zh-CN"}, proof)
    template = semantic_template()
    template["steps"][0]["before"] = {"type": "adb_state", "state": "absent"}
    with pytest.raises(ValueError, match="visible page"):
        compile_plan(["click 1 1"], template, context, proof)


def test_rotated_narrow_controls_use_rotated_pixel_margin():
    target = {"point": [0.5, 0.5], "bounds": [0.49, 0.1, 0.51, 0.9]}
    with pytest.raises(ValueError, match="margin"):
        target_point(target, [0, 0, 1, 1], [2000, 400], rotate_quarters=1, max_error_px=3)
    assert target_point(target, [0, 0, 0.5, 1], [2000, 400], rotate_quarters=0) == (500, 200)


def test_display_parser_rejects_conflicting_viewports():
    line = "Viewport: displayId=1, uniqueId='external:1', orientation=1, logicalFrame=[0, 0, 1920, 1080]"
    assert input_displays(line)[0]["display_id"] == 1
    assert input_displays(line + "\n" + line.replace("external:1", "external:2")) == []


def test_certified_external_display_requires_its_explicit_layout():
    context = reference(display_id=1)["context"]
    proof = certificate(display_id=1)
    plan = compile_plan(["click 1 1"], semantic_template(context), context, proof)
    assert plan["context"]["display_id"] == 1


def test_unattended_semantics_is_bound_to_target_firmware_declaration():
    from foundation.aoa_recovery_policy import require_verified_recovery
    from tests.aoa_fixtures import firmware_declaration

    context, proof = reference()["context"], certificate()
    template = semantic_template()
    template["steps"][0]["before"] = {"type": "visual", "roi": [0, 0, 1, 1],
        "binding_id": "camera-1", "fingerprint": "00" * 256}
    plan = compile_plan(["click 1 1"], template, context, proof)
    options = {"aoa_semantic_plan": plan, "aoa_direct_certificate": proof}
    metadata = {"firmware_compatibility": firmware_declaration(context)}
    require_verified_recovery(["click 1 1"], options, metadata)
    assert options["aoa_firmware_sha256"] == "a" * 64
    metadata["firmware_compatibility"]["context"]["board"] = "other-board"
    with pytest.raises(ValueError, match="firmware environment"):
        require_verified_recovery(["click 1 1"], options, metadata)


@pytest.mark.parametrize("target", [[], {}, False, 1, None])
def test_invalid_target_types_raise_validation_error(target):
    template = semantic_template()
    template["steps"][0]["target"] = target
    with pytest.raises(ValueError):
        compile_plan(["click 1 1"], template, reference()["context"], certificate())


def test_semantic_firmware_requires_visual_precondition_and_matching_capture_binding():
    from foundation.aoa_recovery_policy import require_verified_recovery
    from foundation.aoa_semantics import require_capture_capability
    from tests.aoa_fixtures import firmware_declaration

    context, proof = reference()["context"], certificate()
    plan = compile_plan(["click 1 1"], semantic_template(), context, proof)
    options = {"aoa_semantic_plan": plan, "aoa_direct_certificate": proof}
    with pytest.raises(ValueError, match="前置条件"):
        require_verified_recovery(["click 1 1"], options, {"firmware_compatibility": firmware_declaration(context)})
    with pytest.raises(ValueError, match="外部画面源"):
        require_capture_capability(options, {}, "TARGET", "1-2")
    caps = {"aoa_visual_capture_v1": True, "aoa_capture_bindings": {"TARGET@1-2": {"id": "camera-1", "context": context}}}
    require_capture_capability(options, caps, "TARGET", "1-2")
    with pytest.raises(ValueError):
        require_capture_capability(options, caps, "OTHER", "1-2")
