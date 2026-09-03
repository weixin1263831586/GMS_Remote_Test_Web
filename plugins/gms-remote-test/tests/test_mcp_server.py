#!/usr/bin/env python3
"""Tests for the gms-remote-test stdio MCP adapter.

The adapter is exercised end to end against a stub CLI script: build_argv
and run_cli are checked without the real Controller, and the JSON-RPC loop
is driven through the real subprocess entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))

import mcp_server  # noqa: E402


class BuildArgvTests(unittest.TestCase):
    def test_normalize_command_accepts_prefix_variants(self):
        self.assertEqual(
            mcp_server.normalize_command("gms-rt-devices-list"),
            "gms-rt-devices-list",
        )
        self.assertEqual(
            mcp_server.normalize_command("devices-list"), "gms-rt-devices-list"
        )
        self.assertEqual(
            mcp_server.normalize_command("gms_rt_auth_login"), "gms-rt-auth-login"
        )
        self.assertEqual(
            mcp_server.normalize_command("rt-auth-login"), "gms-rt-auth-login"
        )

    def test_build_argv_with_list_and_string_args(self):
        argv = mcp_server.build_argv("devices-list", ["D1", "--json"])
        self.assertEqual(
            argv, ["bash", str(mcp_server.cli_script()), "gms-rt-devices-list", "D1", "--json"]
        )
        argv = mcp_server.build_argv("devices-info", "D1 --state online")
        self.assertIn("--state", argv)
        self.assertIn("online", argv)

    def test_build_argv_rejects_nested_args(self):
        with self.assertRaises(ValueError):
            mcp_server.build_argv("devices-list", [["D1"]])


class RunCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original_cli = mcp_server.cli_script
        self.cli_path = Path(self._tmp.name) / "gms-remote-test.sh"

    def tearDown(self):
        mcp_server.cli_script = self._original_cli

    def _write_stub(self, body: str) -> None:
        self.cli_path.write_text(f"#!/bin/bash\n{body}\n")
        self.cli_path.chmod(0o755)
        mcp_server.cli_script = lambda: self.cli_path

    def test_denied_interactive_command(self):
        text, is_error = mcp_server.run_cli("gms-rt-terminal-open")
        self.assertTrue(is_error)
        self.assertIn("denied", text)

    def test_run_cli_success_returns_stdout(self):
        self._write_stub('echo \'{"ok":true,"exit_code":0}\'\nexit 0')
        text, is_error = mcp_server.run_cli("gms-rt-devices-list")
        self.assertFalse(is_error)
        self.assertIn('"ok":true', text)

    def test_run_cli_reports_nonzero_exit_as_error(self):
        self._write_stub(
            'echo \'{"ok":false,"exit_code":5}\' >&2\nexit 5'
        )
        text, is_error = mcp_server.run_cli("gms-rt-jobs-cancel", ["J1"])
        self.assertTrue(is_error)
        self.assertIn("5", text)

    def test_stdin_secret_is_forwarded(self):
        received = {}

        def fake_run(*args, **kwargs):
            received["input"] = kwargs.get("input")
            return subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        original = mcp_server.subprocess.run
        mcp_server.subprocess.run = fake_run
        try:
            text, is_error = mcp_server.run_cli(
                "gms-rt-auth-login", ["hcq"], stdin_text="secret\n"
            )
        finally:
            mcp_server.subprocess.run = original
        self.assertFalse(is_error)
        self.assertEqual(received.get("input"), "secret\n")


class JsonRpcLoopTests(unittest.TestCase):
    def _exchange(self, messages):
        """Run the server main() against a scripted stdin and collect replies."""
        stdin_lines = "\n".join(json.dumps(m) for m in messages) + "\n"
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "scripts" / "mcp_server.py")],
            input=stdin_lines,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        replies = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        return replies

    def test_initialize_tools_list_and_unknown_tool(self):
        replies = self._exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "gms-remote-test")
        tool_names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertIn("gms_rt_run", tool_names)
        self.assertIn("gms_rt_test_start", tool_names)
        self.assertEqual(replies[2]["error"]["code"], -32601)
        self.assertEqual(replies[3]["result"], {})

    def test_missing_cli_script_reports_tool_error(self):
        # Point the adapter at a missing CLI: run_cli must return an error
        # text instead of raising out of the tool call.
        with tempfile.TemporaryDirectory() as tmp:
            original = mcp_server.cli_script
            mcp_server.cli_script = lambda: Path(tmp) / "missing.sh"
            try:
                text, is_error = mcp_server.run_cli("gms-rt-devices-list")
            finally:
                mcp_server.cli_script = original
        self.assertTrue(is_error)
        # OSError 路径给出可读错误而非堆栈；错误文本包含启动失败信息。
        self.assertIn("No such file", text)

    def test_tools_list_schema_shape(self):
        replies = self._exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ])
        for tool in replies[0]["result"]["tools"]:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")


class DeniedAndValidationTests(unittest.TestCase):
    def test_run_tool_requires_command(self):
        text, is_error = mcp_server.run_tool({})
        self.assertTrue(is_error)
        self.assertIn("command", text)

    def test_test_start_tool_requires_device(self):
        text, is_error = mcp_server.test_start_tool({})
        self.assertTrue(is_error)
        self.assertIn("device", text)

    def test_test_start_tool_builds_wait_args(self):
        captured = {}

        def fake_run(command, args=None, stdin_text=None, timeout=None):
            captured["command"] = command
            captured["args"] = args
            return '{"ok": true}', False

        original = mcp_server._run_with_json
        mcp_server._run_with_json = fake_run
        try:
            mcp_server.test_start_tool({
                "device": "RK3572", "type": "CTS", "module": "m1",
                "wait": True, "max_wait": 300,
            })
        finally:
            mcp_server._run_with_json = original
        self.assertEqual(captured["command"], "gms-rt-test-start")
        self.assertEqual(
            captured["args"], ["RK3572", "CTS", "m1", "--wait", "--max-wait", "300"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
