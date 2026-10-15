"""MCP argument schemas, cached-client interoperability and routing fences."""

import io
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
sys.path.insert(0, str(RUNTIME))
import mcp_server as server  # noqa: E402


@pytest.fixture
def runner(monkeypatch):
    catalog = {
        "gms-rt-reports-analyze": {"agent_safe_unattended": True},
        "gms-rt-reports-delete": {"agent_safe_unattended": False, "mode": "mutating"},
    }
    monkeypatch.setattr(server, "_load_catalog", lambda *_, **__: catalog)
    run = Mock(return_value=('{"ok":true}', False))
    monkeypatch.setattr(server, "run_cli", run)
    return run


def test_generic_runner_schema_declares_string_array():
    tool = next(t for t in server.mcp_tool_schemas.ALL_TOOLS if t["name"] == "gms_rt_run")
    schema = tool["inputSchema"]["properties"]["args"]
    assert schema["type"] == "array"
    assert schema["items"] == {"type": "string"}
    assert "shell-like" not in schema["description"]


def test_mcp_tool_arguments_all_declare_a_type_for_clients():
    for tool in server.mcp_tool_schemas.ALL_TOOLS:
        for name, schema in tool["inputSchema"].get("properties", {}).items():
            assert any(key in schema for key in ("type", "anyOf", "oneOf", "$ref")), (tool["name"], name)
    logcat = next(t for t in server.mcp_tool_schemas.ALL_TOOLS if t["name"] == "gms_rt_logcat")
    assert logcat["inputSchema"]["properties"]["args"]["anyOf"] == [
        {"type": "array", "items": {"type": "string"}}, {"type": "string"},
    ]


@pytest.mark.parametrize("encoded", [False, True])
def test_real_jsonrpc_call_accepts_array_and_legacy_encoded_array(runner, encoded):
    args = ["2026.10.13_17.21.15.621_7814", "filename with spaces.xml"]
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "gms_rt_run", "arguments": {
            "command": "reports-analyze", "args": json.dumps(args) if encoded else args,
        },
    }}
    output = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(message) + "\n")), patch("sys.stdout", output):
        server.main()
    assert json.loads(output.getvalue())["result"]["isError"] is False
    runner.assert_called_once_with("reports-analyze", args, stdin_text=None, timeout=None)


@pytest.mark.parametrize("args", [
    "", "2026.10.13_17.21.15.621_7814", "[", '"text"', '{"0":"value"}',
    "null", "true", '"[\\"value\\"]"', ["value", 1], [["value"]], 1, {},
])
def test_invalid_argument_shapes_never_execute(runner, args):
    text, error = server.run_tool({"command": "reports-analyze", "args": args})
    assert error is True and "args must be an array of strings" in text
    assert '"args": [' in text
    runner.assert_not_called()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("flag", [
    "--server", "--ca-cert", "--insecure", "--profile", "--token", "--password-stdin",
])
@pytest.mark.parametrize("inline", [False, True])
def test_legacy_decoding_cannot_bypass_transport_or_credential_fences(runner, encoded, flag, inline):
    args = [flag + "=value"] if inline else [flag, "value"]
    text, error = server.run_tool({
        "command": "reports-analyze", "args": json.dumps(args) if encoded else args,
    })
    assert error is True and "cannot override" in text
    runner.assert_not_called()


@pytest.mark.parametrize("encoded", [False, True])
def test_legacy_decoding_does_not_allow_mutating_commands(runner, encoded):
    args = ["report-id"]
    text, error = server.run_tool({
        "command": "reports-delete", "args": json.dumps(args) if encoded else args,
    })
    assert error is True and "not agent-safe" in text
    runner.assert_not_called()
