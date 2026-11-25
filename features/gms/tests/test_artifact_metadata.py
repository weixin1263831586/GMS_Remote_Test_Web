"""Android API metadata contracts for GMS artifacts."""

import pytest

from features.gms.queries import (
    _artifact_api_level,
    _artifact_release_number,
    _artifact_section_url,
)


@pytest.mark.parametrize("version,api", [
    ("Android 13", "33"), ("Android 14", "34"), ("Android 15", "35"),
    ("Android 16", "36"), ("Android 17", "37"), ("Android 14.1", "34"),
])
def test_suite_release_is_not_fractional_platform_api_level(version, api):
    item = {"release_name": version, "source_key": "cts_downloads"}
    release = _artifact_release_number(item)
    assert _artifact_api_level(item, release) == api
    assert _artifact_section_url(item, release).endswith("#android-" + str(int(api) - 20))


def test_gts_api_range_uses_android_major_versions():
    assert _artifact_api_level({
        "suite_type": "GTS", "target_platform": "Android 13 - 17",
    }) == "33-37"
