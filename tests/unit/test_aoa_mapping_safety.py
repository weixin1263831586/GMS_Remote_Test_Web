"""Regression cases for normalized projection, validation evidence and persistence."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from features.devices.aoa_calibrations import AoaCalibrationStore
from foundation.aoa_calibration import TouchMapping, context_matches, convert_point, per_axis_mapping
from tests.aoa_fixtures import verified_mapping


def test_standard_center_survives_cross_resolution_projection():
    assert convert_point(per_axis_mapping(800, 1280), per_axis_mapping(1200, 1920), 180, 320) == (180, 320)


def test_corrected_direct_is_not_standard_and_requires_holdout_samples():
    m = verified_mapping(offset_y=60)
    assert m.verified and m.effective_policy == "direct_calibrated"
    raw = m.to_dict()
    raw["samples"] = [s for s in raw["samples"] if not s["validation"]]
    with pytest.raises(ValueError, match=r"independent|errors"):
        TouchMapping.from_dict(raw)


@pytest.mark.parametrize("change", [
    {"matrix": [1, 0, 2, 0]}, {"samples": [{}]}, {"max_error_px": 7},
    {"mapping_policy": "direct_standard"}, {"display_pixels": [100, 100]},
])
def test_invalid_or_forged_mapping_is_rejected(change):
    with pytest.raises(ValueError):
        TouchMapping.from_dict({**verified_mapping().to_dict(), **change})


@pytest.mark.parametrize('change', [
    {'matrix': [10**1000, 0, 0, 1]}, {'offset': [0, 10**1000]},
    {'rms_error_px': 10**1000}, {'matrix': [1e308, 0, 0, 1e308]},
    {'matrix': [1e308, 1e308, 1e308, 1e308]},
])
def test_numeric_overflow_is_a_validation_error(change):
    with pytest.raises(ValueError):
        TouchMapping.from_dict({**verified_mapping().to_dict(), **change})


@pytest.mark.parametrize('display', [[None, None], ['800', '1280'], [800.1, 1280], [True, 1280], [0, 1280]])
def test_malformed_display_context_never_matches_or_raises(display):
    context = verified_mapping().context
    assert not context_matches(context, {**context, 'display': display})


def test_holdout_failure_invalidates_an_otherwise_exact_fit():
    from foundation.aoa_calibration import mapping_from_samples

    m = verified_mapping()
    train = [(tuple(s["hid"]), tuple(s["cooked"])) for s in m.samples if not s["validation"]]
    validation = [(tuple(s["hid"]), tuple(s["cooked"])) for s in m.samples if s["validation"]]
    validation[0] = (validation[0][0], (validation[0][1][0] + 20, validation[0][1][1]))
    failed = mapping_from_samples("direct", train, context=m.context,
        display_pixels=m.display_pixels, validation_samples=validation)
    assert not failed.verified and failed.effective_policy == "unsupported"


def test_save_reload_match_is_owner_and_environment_bound(tmp_path):
    m = verified_mapping()
    store = AoaCalibrationStore(tmp_path)
    saved = store.save_calibration("owner", m)
    assert AoaCalibrationStore(tmp_path).find_matching("owner", m.context)["id"] == saved["id"]
    assert store.find_matching("other", m.context) is None
    for key in ("fingerprint", "density", "kernel", "display_route"):
        context = copy.deepcopy(m.context)
        context.pop(key)
        assert not context_matches(m.context, context)
        assert store.find_matching("owner", context) is None


def test_reprojection_rejects_unhandled_rotation_and_missing_display():
    m = verified_mapping()
    other = verified_mapping()
    other.context["rotation"] = 1
    with pytest.raises(ValueError, match="rotation"):
        convert_point(m, other, 180, 320)
    with pytest.raises(ValueError, match="display dimensions"):
        convert_point(TouchMapping("direct", (1, 0, 0, 1), (0, 0)), m, 180, 320)


def test_concurrent_replacement_returns_each_successful_save_receipt(tmp_path):
    stores = [AoaCalibrationStore(tmp_path) for _ in range(4)]
    mapping = verified_mapping()

    def save(index):
        receipt = stores[index % len(stores)].save_calibration('owner', mapping, label=str(index))
        assert receipt['mapping']['label'] == str(index)
        return receipt['id']

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(save, range(24)))
    assert len(set(ids)) == 24
    assert len(stores[0].list_calibrations('owner')) == 1
