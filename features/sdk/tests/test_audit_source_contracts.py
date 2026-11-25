"""SDK management errors are typed contracts."""
import json

from features.sdk.source_manage_api import _error_response
from features.sdk.source_user_store import UserSourceStoreError


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
