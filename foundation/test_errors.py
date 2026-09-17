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
