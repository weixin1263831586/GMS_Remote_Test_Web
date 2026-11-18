"""CodeSearch MCP requests must not hold the JSON-RPC server indefinitely."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts/mcp_server.py"
SPEC = importlib.util.spec_from_file_location("codesearch_mcp_adapter_under_test", MODULE)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def test_codesearch_mcp_search_runs_bounded_subprocess_not_inprocess():
    with patch.object(adapter.subprocess, "run", return_value=SimpleNamespace(
        stdout=b"result_count: 1\nframeworks/base/foo.java:42",
        stderr=b"", returncode=0
    )) as spawned:
        output, is_error = adapter.run_codesearch(["search", "--keywords", "Binder"])
    assert not is_error
    assert "frameworks/base/foo.java:42" in output
    command = spawned.call_args.args[0]
    assert command[0] and command[1].endswith("codesearch.py")
    assert command[2:] == ["search", "--keywords", "Binder"]
    assert spawned.call_args.kwargs["timeout"] == 75
    assert spawned.call_args.kwargs["stdin"] is subprocess.DEVNULL
    assert spawned.call_args.kwargs["capture_output"] is True


def test_codesearch_mcp_timeout_is_explicit_tool_error():
    with patch.object(adapter.subprocess, "run", side_effect=subprocess.TimeoutExpired(
        cmd=["python", "codesearch.py"], timeout=75
    )):
        text, is_error = adapter.run_codesearch(["list_projects"])
    assert is_error
    assert "timed out after 75 seconds" in text


def test_codesearch_mcp_failed_client_does_not_hide_failure():
    with patch.object(adapter.subprocess, "run", return_value=SimpleNamespace(
        stdout=b"", stderr=b"Remote index unavailable", returncode=7
    )):
        text, is_error = adapter.run_codesearch(["search", "--keywords", "foo"])
    assert is_error
    assert "Remote index unavailable" in text


def test_mcp_tool_arguments_reject_missing_keywords_and_bad_limits():
    assert adapter.search_tool({})[1] is True
    assert adapter.search_tool({"keywords": "x", "limit": "oops"})[1] is True


def test_codesearch_client_forbids_http_redirects_and_oversized_body():
    """Prevent bearer-token forwarding and unbounded indexing responses."""
    client_path = MODULE.with_name("codesearch.py")
    spec = importlib.util.spec_from_file_location("codesearch_client_security_test", client_path)
    assert spec and spec.loader
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    handler = client._NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Moved", {}, "https://evil.invalid/") is None

    class _Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def read(self, count):
            assert count == client.MAX_RESPONSE_BYTES + 1
            return b"x" * count

    class _Opener:
        def open(self, request, timeout):
            assert timeout == 60
            assert request.get_header("Authorization") == "Bearer secret"
            return _Response()

    with patch.object(client, "MAX_RESPONSE_BYTES", 8), patch.object(client, "load_config", return_value={
        "base_url": "https://source.example.test", "token": "secret",
        "default_projects": [], "default_limit": 15
    }), patch.object(client.urllib.request, "build_opener", return_value=_Opener()):
        import pytest
        with pytest.raises(ValueError, match="exceeds 32 MiB"):
            client.request("/api/v1/search")
