"""Android API metadata and SDK management errors are typed contracts."""
import json

import pytest

from features.system.source_manage_api import _error_response
from features.system.source_user_store import UserSourceStoreError
from features.system.update_monitor.api_support import (
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


def test_sdk_user_validation_error_preserves_safe_semantics():
    response = _error_response(UserSourceStoreError("非法源码目录", status_code=422))
    assert response.status_code == 422
    assert json.loads(response.body)["code"] == "INVALID_SEMANTICS"
    assert json.loads(response.body)["error"] == "非法源码目录"


def test_unexpected_sdk_error_does_not_expose_credentials_or_paths():
    response = _error_response(RuntimeError("password=SECRET /home/private/keys"))
    assert response.status_code == 500
    assert json.loads(response.body)["code"] == "INTERNAL_ERROR"
    assert "SECRET" not in response.body.decode()
    assert "/home/private" not in response.body.decode()
