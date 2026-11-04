import asyncio
import json

from foundation.errors import handle_api_errors


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def test_sync_unexpected_exception_is_not_exposed_to_client():
    secret = "ssh://user:password@example.invalid/private/path"

    @handle_api_errors
    def explode():
        raise RuntimeError(secret)

    response = explode()
    payload = _payload(response)

    assert response.status_code == 500
    assert payload["success"] is False
    assert payload["error"] == "Internal server error"
    assert secret not in response.body.decode("utf-8")


def test_async_unexpected_exception_is_not_exposed_to_client():
    secret = "/home/private/controller/configs/secrets/token.json"

    @handle_api_errors
    async def explode():
        raise RuntimeError(secret)

    response = asyncio.run(explode())
    payload = _payload(response)

    assert response.status_code == 500
    assert payload["success"] is False
    assert payload["error"] == "Internal server error"
    assert secret not in response.body.decode("utf-8")


def test_request_model_validation_preserves_422_envelope():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from foundation.error_model import register_api_error_handler

    class Body(BaseModel):
        count: int

    app = FastAPI()
    register_api_error_handler(app)

    @app.post("/validate")
    def validate(body: Body):
        return {"success": True}

    with TestClient(app) as client:
        response = client.post("/validate", json={"count": "private-invalid-value"})
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_SEMANTICS"
    assert "private-invalid-value" not in response.text


def test_internal_device_action_port_maps_validation_to_422():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import pytest

    from features.cluster import _port_device_action_request
    from foundation.error_model import ApiError

    with patch("features.cluster.device_action", new_callable=AsyncMock) as dispatch, pytest.raises(ApiError) as caught:
        asyncio.run(_port_device_action_request(SimpleNamespace(), {
            "worker_id": "w", "devices": ["A"], "action": "aoa_sequence", "wait_adb_seconds": 601,
        }))
    dispatch.assert_not_called()
    assert caught.value.to_response().status_code == 422
    assert "wait_adb_seconds" in caught.value.message
