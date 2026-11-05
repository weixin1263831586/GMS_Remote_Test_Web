"""AOA 多点仿射标定引擎与 mapping_policy 的机制测试（审核意见 1.txt）。"""

from __future__ import annotations

import pytest

from foundation.aoa_calibration import (
    MAPPING_POLICY_DIRECT_CALIBRATED,
    MAPPING_POLICY_DIRECT_UNVERIFIED,
    MAPPING_POLICY_POINTER_CALIBRATED,
    MAPPING_POLICY_UNSUPPORTED,
    MAX_CALIBRATION_ERROR_PX,
    TouchMapping,
    context_matches,
    convert_commands,
    convert_point,
    fit_affine,
    five_point_calibration_points,
    mapping_from_samples,
    per_axis_mapping,
    policy_for,
    validate_mapping_policy,
)


def test_aosp_per_axis_formula_uses_max_minus_min_plus_one():
    """按轴估算分母必须是 HID Logical Maximum + 1（AOSP touch-devices 公式）。"""
    mapping = per_axis_mapping(800, 1280)
    assert mapping.matrix[0] == pytest.approx(800 / 361)
    assert mapping.matrix[3] == pytest.approx(1280 / 641)
    assert mapping.mapping_policy == MAPPING_POLICY_DIRECT_UNVERIFIED
    assert not mapping.verified


def test_real_to_aoa_point_matches_aosp_inverse():
    from foundation.aoa_profile import AOA_TOUCH_MAX_X, AOA_TOUCH_MAX_Y, real_to_aoa_point

    x, y = real_to_aoa_point(400, 640, 800, 1280)
    assert x == round(400 * (AOA_TOUCH_MAX_X + 1) / 800)
    assert y == round(640 * (AOA_TOUCH_MAX_Y + 1) / 1280)
    # 屏幕右下角换算后 clamp 在描述符范围内。
    assert real_to_aoa_point(800, 1280, 800, 1280) == (AOA_TOUCH_MAX_X, AOA_TOUCH_MAX_Y)


def test_five_point_layout_spreads_corners_and_center():
    points = five_point_calibration_points(360, 640)
    assert len(points) == 5
    (x1, y1), (x2, y2), (cx, cy), (x3, y3), (x4, y4) = points
    assert (cx, cy) == (180, 320)
    assert (x1, y1) == (90, 160) and (x2, y2) == (270, 160)
    assert (x3, y3) == (90, 480) and (x4, y4) == (270, 480)
    # 三个以上不共线点：任取三点面积非零。
    assert (x2 - x1) * (cy - y1) - (y2 - y1) * (cx - x1) != 0


def test_fit_affine_recovers_scale_and_offset():
    """精确仿射（uniform scale + 偏移）应被最小二乘完整还原。"""
    scale, offset = 2.216, (3.0, -5.0)
    hid_points = five_point_calibration_points(360, 640)
    samples = [((u, v), (u * scale + offset[0], v * scale + offset[1]))
               for u, v in hid_points]
    matrix, off = fit_affine(samples)
    assert matrix == pytest.approx((scale, 0.0, 0.0, scale), abs=1e-9)
    assert off == pytest.approx(offset, abs=1e-9)


def test_fit_affine_recovers_axis_coupling_single_point_cannot():
    """X 比例同时作用于 Y（实机 8e4d 统一 2.216）+ Y 偏移：单点比例必错，仿射拟合恢复。"""
    scale = 2.216
    offset_y = 69.0  # 审核意见示例：中心点 Y 偏差约 69px。
    hid_points = five_point_calibration_points(360, 640)
    samples = [((u, v), (u * scale, v * scale + offset_y)) for u, v in hid_points]
    matrix, off = fit_affine(samples)
    assert matrix == pytest.approx((scale, 0.0, 0.0, scale), abs=1e-9)
    assert off == pytest.approx((0.0, offset_y), abs=1e-9)


def test_fit_affine_rejects_collinear_samples():
    with pytest.raises(ValueError, match="collinear"):
        fit_affine([((0, 0), (0, 0)), ((1, 1), (2, 2)), ((2, 2), (4, 4))])


def test_fit_affine_requires_three_samples():
    with pytest.raises(ValueError, match="at least 3"):
        fit_affine([((0, 0), (0, 0)), ((180, 320), (400, 709))])


def test_mapping_from_samples_reports_residuals_and_policy():
    scale = 2.216
    hid_points = five_point_calibration_points(360, 640)
    samples = [((u, v), (u * scale, v * scale)) for u, v in hid_points]
    # 注入 1px 噪声到中心点。
    samples[2] = (samples[2][0], (samples[2][1][0] + 1.0, samples[2][1][1]))
    mapping = mapping_from_samples("direct", samples, display_pixels=(800, 1280),
        validation_samples=[((120, 213), (120 * scale, 213 * scale)),
                            ((240, 427), (240 * scale, 427 * scale))])
    assert len(mapping.samples) == 7
    assert mapping.max_error_px <= MAX_CALIBRATION_ERROR_PX
    assert mapping.verified
    assert mapping.effective_policy == MAPPING_POLICY_DIRECT_CALIBRATED
    assert mapping.rms_error_px > 0


def test_mapping_with_large_residual_is_not_verified():
    scale = 2.216
    hid_points = five_point_calibration_points(360, 640)
    samples = [((u, v), (u * scale, v * scale)) for u, v in hid_points]
    samples[2] = (samples[2][0], (samples[2][1][0] + 40.0, samples[2][1][1]))
    mapping = mapping_from_samples("direct", samples)
    assert mapping.max_error_px > MAX_CALIBRATION_ERROR_PX
    assert not mapping.verified


def test_policy_helpers():
    assert policy_for("direct", True) == MAPPING_POLICY_DIRECT_CALIBRATED
    assert policy_for("pointer", True) == MAPPING_POLICY_POINTER_CALIBRATED
    assert policy_for("direct", False) == MAPPING_POLICY_UNSUPPORTED
    assert validate_mapping_policy(None) == ""
    assert validate_mapping_policy("") == ""
    assert validate_mapping_policy("direct_standard") == "direct_standard"
    with pytest.raises(ValueError, match="mapping policy"):
        validate_mapping_policy("magic")


def test_unsupported_policy_blocks_auto_touch():
    from foundation.aoa_calibration import auto_touch_allowed

    assert auto_touch_allowed("unsupported") is False
    assert auto_touch_allowed("direct_standard") is True
    assert auto_touch_allowed("") is True


def test_per_axis_mapping_rejects_verified_policies():
    with pytest.raises(ValueError, match="per-axis"):
        per_axis_mapping(800, 1280, mapping_policy="direct_standard")
    with pytest.raises(ValueError, match="display size"):
        per_axis_mapping(0, 1280)


def test_touch_mapping_roundtrip_and_clamp():
    mapping = per_axis_mapping(800, 1280)
    u, v = mapping.screen_to_hid(400, 640)
    assert 0 <= u <= 360 and 0 <= v <= 640
    # 非可逆矩阵不能反解。
    with pytest.raises(ValueError, match="invertible"):
        TouchMapping(hid_touch_mode="direct", matrix=(1.0, 0.0, 2.0, 0.0), offset=(0.0, 0.0))


def test_touch_mapping_serialization_roundtrip():
    mapping = mapping_from_samples(
        "direct",
        [((u, v), (u * 2.2, v * 2.2))
         for u, v in five_point_calibration_points(360, 640)],
        context={"model": "RK3576", "release": "16", "display": [800, 1280],
                 "rotation": 0, "hid_touch_mode": "direct"},
        display_pixels=(800, 1280),
    )
    restored = TouchMapping.from_dict(mapping.to_dict())
    assert restored.matrix == pytest.approx(mapping.matrix, abs=1e-6)
    assert restored.hid_touch_mode == mapping.hid_touch_mode
    assert restored.context == mapping.context
    assert restored.verified == mapping.verified


def test_touch_mapping_from_dict_validates_payload():
    good = per_axis_mapping(800, 1280).to_dict()
    assert TouchMapping.from_dict(good).hid_touch_mode == "direct"
    for mutate, message in (
        ({"hid_touch_mode": "magic"}, "hid_touch_mode"),
        ({"hid_touch_mode": None}, "hid_touch_mode"),
        ({"matrix": [1, 2, 3]}, "matrix"),
        ({"matrix": [1, 2, 3, "x"]}, "matrix"),
        ({"offset": [1]}, "offset"),
        ({"display_pixels": [-1, 0]}, "display_pixels"),
        ({"samples": "all"}, "samples"),
        ({"context": []}, "context"),
        ({"rms_error_px": -1}, "non-negative"),
        ({"mapping_policy": "magic"}, "mapping policy"),
    ):
        payload = {**good, **mutate}
        with pytest.raises(ValueError, match=message):
            TouchMapping.from_dict(payload)


def test_convert_commands_reprojects_touch_coordinates():
    """同屏比例差异：源 800×1280（uniform 2.216）→ 目标 1200×1920 按轴 361/641。"""
    source = TouchMapping(hid_touch_mode="direct", matrix=(2.216, 0.0, 0.0, 2.216),
                          offset=(0.0, 0.0), display_pixels=(800, 1280))
    target = per_axis_mapping(1200, 1920)
    center = convert_point(source, target, 180, 320)
    assert center == (round(180 * 2.216 * 361 / 800), round(320 * 2.216 * 641 / 1280))
    converted = convert_commands(
        ["click 180 320", "swipe 90 480 500 270 160", "wake", "key TAB", "sleep 100", "write abc"],
        source, target)
    assert converted[0] == f"click {center[0]} {center[1]}"
    assert converted[1].startswith("swipe ")
    assert converted[2:] == ["wake", "key TAB", "sleep 100", "write abc"]
    # 转换结果仍须通过官方语法边界。
    from foundation.aoa_profile import parse_profile_commands

    parse_profile_commands(converted)


def test_convert_commands_rejects_malformed_input():
    source = per_axis_mapping(800, 1280)
    with pytest.raises(ValueError, match="invalid AOA action"):
        convert_commands(["click 1 2 3"], source, source)
    with pytest.raises(ValueError, match="invalid AOA action"):
        convert_commands(["swipe 1 2 3"], source, source)


def test_context_matches_requires_full_environment_equality():
    record = {"model": "RK3576", "release": "16", "rotation": 0,
              "hid_touch_mode": "direct", "display": [800, 1280]}
    assert context_matches(record, dict(record))
    for key, value in (("model", "RK3562"), ("release", "15"), ("rotation", 1),
                       ("hid_touch_mode", "pointer"), ("display", [1200, 1920])):
        assert not context_matches(record, {**record, key: value})
    assert not context_matches(record, {k: v for k, v in record.items() if k != "display"})


def test_worker_calibration_probe_points_use_five_point_layout():
    from worker_agent.aoa_actions import CALIBRATION_PROBE_COUNT, calibration_probe_points

    probes = calibration_probe_points()
    assert len(probes) == CALIBRATION_PROBE_COUNT == 5
    assert all(0 <= x <= 360 and 0 <= y <= 640 for x, y in probes)
