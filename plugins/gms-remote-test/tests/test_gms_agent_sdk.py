"""GMS Agent Runtime SDK tests (10.txt §五, Phase 2).

The SDK must produce CLI-compatible envelopes, map HTTP status codes to the
CLI's stable exit codes, and the MCP fast path must degrade to the CLI
whenever the SDK is unavailable.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "gms-remote-test" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from gms_agent import GmsApiError, GmsClient  # noqa: E402
from gms_agent.client import (  # noqa: E402
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_OPERATION,
    EXIT_PERMISSION,
    _http_exit_code,
)


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("GMS_REMOTE_TEST_SERVER", "https://controller:5001")
    monkeypatch.delenv("GMS_AUTH_TOKEN_FILE", raising=False)
    return GmsClient(timeout=5)


def test_http_exit_code_mapping_matches_cli():
    # CLI _http_exit_code: 401→3, 403→4, 409/423/429→5, 000/5xx→6, 2xx→0.
    assert _http_exit_code(200) == EXIT_OK
    assert _http_exit_code(401) == EXIT_AUTH
    assert _http_exit_code(403) == EXIT_PERMISSION
    for status in (409, 423, 429):
        assert _http_exit_code(status) == EXIT_CONFLICT
    assert _http_exit_code(500) == EXIT_NETWORK
    assert _http_exit_code(503) == EXIT_NETWORK
    assert _http_exit_code(404) == EXIT_OPERATION


def test_missing_server_url_raises_usage_error(monkeypatch):
    monkeypatch.delenv("GMS_REMOTE_TEST_SERVER", raising=False)
    with pytest.raises(GmsApiError) as excinfo:
        GmsClient()
    assert excinfo.value.exit_code == 2


def test_request_success_envelope(server, monkeypatch):
    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({"success": True, "workers": []}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    captured = {}

    def fake_urlopen(request, timeout=0, context=None):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    envelope = server.request("GET", "/cluster/workers")
    assert envelope == {
        "ok": True,
        "exit_code": EXIT_OK,
        "data": {"success": True, "workers": []},
    }
    assert captured["url"] == "https://controller:5001/api/cluster/workers"


def test_request_403_raises_permission_error(server, monkeypatch):
    def fake_urlopen(request, timeout=0, context=None):
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(GmsApiError) as excinfo:
        server.request("GET", "/cluster/workers")
    assert excinfo.value.exit_code == EXIT_PERMISSION


def test_token_file_is_read_never_password(monkeypatch, tmp_path):
    token_file = tmp_path / "agent.token"
    token_file.write_text("secret-token\n")
    monkeypatch.setenv("GMS_REMOTE_TEST_SERVER", "https://controller:5001")
    monkeypatch.setenv("GMS_AUTH_TOKEN_FILE", str(token_file))
    client = GmsClient()
    assert client.token == "secret-token"
    # The SDK surface must not expose any password parameter at all.
    import inspect

    params = set(inspect.signature(GmsClient.__init__).parameters)
    assert "password" not in params
    assert "username" not in params


def test_mcp_fast_path_falls_back_without_server(monkeypatch):
    spec = importlib.util.spec_from_file_location("mcp_server_under_test", SCRIPTS / "mcp_server.py")
    mcp = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("mcp_server_under_test", mcp)
    spec.loader.exec_module(mcp)
    monkeypatch.delenv("GMS_REMOTE_TEST_SERVER", raising=False)
    # No server configured -> SDK client cannot be built -> None (CLI path).
    assert mcp._sdk_fast_call("gms-rt-jobs-list", ["100"]) is None


def test_mcp_fast_path_rejects_denied_commands():
    spec = importlib.util.spec_from_file_location("mcp_server_under_test2", SCRIPTS / "mcp_server.py")
    mcp = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("mcp_server_under_test2", mcp)
    spec.loader.exec_module(mcp)
    # Denied commands never reach the SDK fast path.
    assert "gms-rt-terminal-open" in mcp._DENIED_COMMANDS
    assert "gms-rt-terminal-open" not in mcp._SDK_CLI_ROUTES
