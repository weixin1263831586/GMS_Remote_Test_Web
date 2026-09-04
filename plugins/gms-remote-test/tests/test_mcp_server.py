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


PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))

import mcp_server  # noqa: E402


def _reset_catalog_cache() -> None:
    mcp_server._CATALOG_CACHE["loaded_at"] = 0.0
    mcp_server._CATALOG_CACHE["commands"] = None


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

    def test_build_argv_injects_json_flags(self):
        argv = mcp_server.build_argv("devices-list", ["D1"])
        self.assertEqual(
            argv,
            [
                "bash", str(mcp_server.cli_script()), "gms-rt-devices-list",
                "D1", "--json", "--non-interactive",
            ],
        )

    def test_build_argv_does_not_duplicate_injected_flags(self):
        argv = mcp_server.build_argv(
            "devices-list", ["--json", "--non-interactive"]
        )
        self.assertEqual(argv.count("--json"), 1)
        self.assertEqual(argv.count("--non-interactive"), 1)

    def test_build_argv_accepts_string_args(self):
        argv = mcp_server.build_argv("devices-info", "D1 --state online")
        self.assertIn("--state", argv)
        self.assertIn("online", argv)

    def test_build_argv_rejects_nested_args(self):
        with self.assertRaises(ValueError):
            mcp_server.build_argv("devices-list", [["D1"]])


class CompactEnvelopeTests(unittest.TestCase):
    def test_success_drops_command_and_zero_exit_code(self):
        text = mcp_server._compact_envelope(
            '{"ok":true,"command":"gms-rt-devices-list","exit_code":0,'
            '"data":{"devices":[{"serial":"D1"}]}}'
        )
        self.assertIsNotNone(text)
        payload = json.loads(text)
        self.assertEqual(payload, {"ok": True, "data": {"devices": [{"serial": "D1"}]}})

    def test_success_prunes_empty_fields(self):
        text = mcp_server._compact_envelope(
            '{"ok":true,"command":"gms-rt-devices-list","exit_code":0,'
            '"data":{"count":2,"devices":[{"serial":"D1","state":null,'
            '"tags":[],"note":"","meta":{"deep":{}}}],"cursor":null}}'
        )
        payload = json.loads(text)
        self.assertEqual(
            payload,
            {"ok": True, "data": {"count": 2, "devices": [{"serial": "D1"}]}},
        )

    def test_error_keeps_exit_code_and_diagnostics(self):
        text = mcp_server._compact_envelope(
            '{"ok":false,"command":"gms-rt-devices-list","exit_code":3,'
            '"data":{"error":"Authentication required"},'
            '"diagnostics":"need login"}'
        )
        payload = json.loads(text)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "exit_code": 3,
                "hint": "authenticate with gms_rt_auth_login",
                "data": {"error": "Authentication required"},
                "diagnostics": "need login",
            },
        )

    def test_error_hint_covers_each_documented_exit_code(self):
        for code in (2, 3, 4, 5, 6, 7):
            text = mcp_server._compact_envelope(
                f'{{"ok":false,"exit_code":{code},"data":{{"error":"x"}}}}'
            )
            payload = json.loads(text)
            self.assertIn("hint", payload, f"exit_code {code} lacks a hint")
            self.assertTrue(payload["hint"])

    def test_unknown_exit_code_gets_no_hint(self):
        text = mcp_server._compact_envelope('{"ok":false,"exit_code":9}')
        payload = json.loads(text)
        self.assertNotIn("hint", payload)

    def test_non_envelope_returns_none(self):
        self.assertIsNone(mcp_server._compact_envelope("📱 Listing devices..."))
        self.assertIsNone(mcp_server._compact_envelope(""))


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

    def test_run_cli_success_compacts_envelope(self):
        self._write_stub(
            'echo \'{"ok":true,"command":"gms-rt-devices-list",'
            '"exit_code":0,"data":{"count":2}}\'\nexit 0'
        )
        text, is_error = mcp_server.run_cli("gms-rt-devices-list")
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload, {"ok": True, "data": {"count": 2}})
        self.assertNotIn("command", payload)

    def test_run_cli_passes_injected_flags_to_the_cli(self):
        received = {}

        def fake_run(argv, **kwargs):
            received["argv"] = argv
            return subprocess.CompletedProcess(
                argv, 0,
                stdout='{"ok":true,"exit_code":0,"data":{}}', stderr="",
            )

        original = mcp_server.subprocess.run
        mcp_server.subprocess.run = fake_run
        try:
            _text, is_error = mcp_server.run_cli("gms-rt-devices-list", ["D1"])
        finally:
            mcp_server.subprocess.run = original
        self.assertFalse(is_error)
        argv = received["argv"]
        self.assertIn("--json", argv)
        self.assertIn("--non-interactive", argv)

    def test_run_cli_reports_nonzero_exit_as_error(self):
        self._write_stub(
            'echo \'{"ok":false,"exit_code":5,"data":{"error":"busy"}}\'\nexit 5'
        )
        text, is_error = mcp_server.run_cli("gms-rt-jobs-wait", ["J1"])
        self.assertTrue(is_error)
        payload = json.loads(text)
        self.assertEqual(payload["exit_code"], 5)
        self.assertEqual(payload["ok"], False)

    def test_run_cli_falls_back_to_text_for_non_envelope_output(self):
        self._write_stub("printf 'plain human output\\n'")
        text, is_error = mcp_server.run_cli("gms-rt-system-version")
        self.assertFalse(is_error)
        self.assertIn("plain human output", text)

    def test_stdin_secret_is_forwarded(self):
        received = {}

        def fake_run(*args, **kwargs):
            received["input"] = kwargs.get("input")
            return subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        original = mcp_server.subprocess.run
        mcp_server.subprocess.run = fake_run
        try:
            _text, is_error = mcp_server.run_cli(
                "gms-rt-auth-login", ["hcq"], stdin_text="secret\n"
            )
        finally:
            mcp_server.subprocess.run = original
        self.assertFalse(is_error)
        self.assertEqual(received.get("input"), "secret\n")


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        _reset_catalog_cache()
        self.addCleanup(_reset_catalog_cache)
        self._original_cli = mcp_server.cli_script
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cli_path = Path(self._tmp.name) / "gms-remote-test.sh"
        self.calls = 0

    def tearDown(self):
        mcp_server.cli_script = self._original_cli

    def _write_catalog_stub(self) -> None:
        envelope = json.dumps({
            "ok": True,
            "command": "gms-rt-system-commands",
            "exit_code": 0,
            "data": {
                "commands": [
                    {
                        "name": "gms-rt-devices-list",
                        "mode": "read_only",
                        "usage": "gms-rt-devices-list",
                        "category": "devices",
                        "requires_elevation": False,
                        "agent_safe_unattended": True,
                    },
                    {
                        "name": "gms-rt-burn-firmware",
                        "mode": "mutating",
                        "usage": "gms-rt-burn-firmware <fw> <dev>",
                        "category": "burn",
                        "requires_elevation": True,
                        "agent_safe_unattended": False,
                    },
                ]
            },
        })
        body = (
            "calls_file=$(mktemp)\n"
            "echo x >> \"$(dirname \"$0\")/calls.log\"\n"
            f"cat <<'EOJ'\n{envelope}\nEOJ\n"
        )
        self.cli_path.write_text(f"#!/bin/bash\n{body}\n")
        self.cli_path.chmod(0o755)
        mcp_server.cli_script = lambda: self.cli_path

    def _call_count(self) -> int:
        log = self.cli_path.parent / "calls.log"
        return log.read_text().count("x") if log.exists() else 0

    def test_catalog_is_cached_across_calls(self):
        self._write_catalog_stub()
        first = mcp_server._load_catalog(force=True)
        second = mcp_server._load_catalog()
        self.assertIsNotNone(first)
        self.assertIs(first, second)
        self.assertEqual(self._call_count(), 1)

    def test_run_tool_denies_non_agent_safe_command(self):
        self._write_catalog_stub()
        mcp_server._load_catalog(force=True)
        text, is_error = mcp_server.run_tool(
            {"command": "gms-rt-burn-firmware", "args": ["fw.zip", "D1"]}
        )
        self.assertTrue(is_error)
        self.assertIn("denied", text)
        self.assertIn("mutating", text)

    def test_run_tool_allows_agent_safe_command(self):
        self._write_catalog_stub()
        mcp_server._load_catalog(force=True)
        _text, is_error = mcp_server.run_tool({"command": "gms-rt-devices-list"})
        self.assertFalse(is_error)

    def test_run_tool_suggests_close_commands_for_unknown(self):
        self._write_catalog_stub()
        mcp_server._load_catalog(force=True)
        text, is_error = mcp_server.run_tool({"command": "gms-rt-device-list"})
        self.assertTrue(is_error)
        self.assertIn("unknown command", text)
        self.assertIn("gms-rt-devices-list", text)

    def test_commands_tool_renders_compact_inventory(self):
        self._write_catalog_stub()
        mcp_server._load_catalog(force=True)
        text, is_error = mcp_server.commands_tool({})
        self.assertFalse(is_error)
        self.assertIn("gms-rt-devices-list | read_only | - |", text)
        self.assertIn("gms-rt-burn-firmware | mutating | elev manual |", text)
        self.assertIn("columns: name | mode | flags | usage", text)

    def test_commands_tool_omits_fallback_usage(self):
        # "<name> [arguments]" is the CLI's no-usage fallback and carries no
        # information; the inventory must drop it to save tokens.
        self._write_catalog_stub()
        mcp_server._load_catalog(force=True)
        mcp_server._CATALOG_CACHE["commands"]["gms-rt-devices-list"]["usage"] = (
            "gms-rt-devices-list [arguments]"
        )
        text, _is_error = mcp_server.commands_tool({})
        line = next(
            line_text
            for line_text in text.splitlines()
            if line_text.startswith("gms-rt-devices-list")
        )
        self.assertEqual(line, "gms-rt-devices-list | read_only | -")

    def test_commands_tool_group_filter(self):
        self._write_catalog_stub()
        mcp_server._load_catalog(force=True)
        text, _is_error = mcp_server.commands_tool({"group": "burn"})
        self.assertIn("gms-rt-burn-firmware", text)
        self.assertNotIn("gms-rt-devices-list", text)

    def test_describe_tool_serves_from_catalog(self):
        self._write_catalog_stub()
        mcp_server._load_catalog(force=True)
        text, is_error = mcp_server.describe_tool({"command": "burn-firmware"})
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload["name"], "gms-rt-burn-firmware")


class TypedToolTests(unittest.TestCase):
    def setUp(self):
        _reset_catalog_cache()
        self.addCleanup(_reset_catalog_cache)

    def _capture_run(self):
        captured = {}

        def fake_run(command, args=None, stdin_text=None, timeout=None):
            captured["command"] = command
            captured["args"] = args
            captured["stdin_text"] = stdin_text
            return '{"ok":true,"exit_code":0,"data":{}}', False

        original = mcp_server.run_cli
        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        return captured

    def test_run_tool_requires_command(self):
        text, is_error = mcp_server.run_tool({})
        self.assertTrue(is_error)
        self.assertIn("command", text)

    def test_run_tool_supports_timeout(self):
        captured = self._capture_run()
        mcp_server._CATALOG_CACHE["commands"] = {
            "gms-rt-devices-list": {"agent_safe_unattended": True}
        }
        _text, is_error = mcp_server.run_tool(
            {"command": "devices-list", "timeout": 30}
        )
        self.assertFalse(is_error)
        self.assertEqual(captured["command"], "devices-list")

    def test_test_start_tool_requires_device(self):
        text, is_error = mcp_server.test_start_tool({})
        self.assertTrue(is_error)
        self.assertIn("device", text)

    def test_test_start_tool_builds_wait_args(self):
        captured = self._capture_run()
        mcp_server.test_start_tool({
            "device": "RK3572", "type": "CTS", "module": "m1",
            "wait": True, "max_wait": 300,
        })
        self.assertEqual(captured["command"], "gms-rt-test-start")
        self.assertEqual(
            captured["args"], ["RK3572", "CTS", "m1", "--wait", "--max-wait", "300"]
        )

    def test_test_start_tool_retry_mode(self):
        captured = self._capture_run()
        mcp_server.test_start_tool({
            "retry": "2026.04.11_17.27.04.421_2920",
            "device": "c3d9b8674f4b94f6",
            "type": "GTS",
            "module": "ignored-in-retry-mode",
            "wait": True, "max_wait": 600,
        })
        self.assertEqual(captured["command"], "gms-rt-test-start")
        self.assertEqual(
            captured["args"],
            [
                "--retry", "2026.04.11_17.27.04.421_2920",
                "c3d9b8674f4b94f6", "GTS", "--wait", "--max-wait", "600",
            ],
        )

    def test_test_start_tool_accepts_retry_without_device(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.test_start_tool({
            "retry": "2026.04.11_17.27.04.421_2920",
        })
        self.assertFalse(is_error)
        self.assertEqual(captured["args"], ["--retry", "2026.04.11_17.27.04.421_2920"])

    def test_jobs_list_tool_defaults_to_no_args(self):
        captured = self._capture_run()
        mcp_server.jobs_list_tool({})
        self.assertEqual(captured["command"], "gms-rt-jobs-list")
        self.assertEqual(captured["args"], [])

    def test_jobs_list_tool_passes_limit(self):
        captured = self._capture_run()
        mcp_server.jobs_list_tool({"limit": 5})
        self.assertEqual(captured["args"], ["5"])

    def test_jobs_list_tool_validates_limit(self):
        text, is_error = mcp_server.jobs_list_tool({"limit": "many"})
        self.assertTrue(is_error)
        self.assertIn("limit", text)

    def test_auth_login_tool_requires_both_arguments(self):
        text, is_error = mcp_server.auth_login_tool({"username": "hcq"})
        self.assertTrue(is_error)
        self.assertIn("password_stdin", text)
        text, is_error = mcp_server.auth_login_tool(
            {"username": "hcq", "password_stdin": ""}
        )
        self.assertTrue(is_error)

    def test_auth_login_tool_forwards_password_over_stdin_only(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.auth_login_tool(
            {"username": "hcq", "password_stdin": "s3cret"}
        )
        self.assertFalse(is_error)
        self.assertEqual(captured["command"], "gms-rt-auth-login")
        self.assertEqual(captured["args"], ["hcq", "--password-stdin"])
        self.assertEqual(captured["stdin_text"], "s3cret\n")
        self.assertNotIn("s3cret", json.dumps(captured["args"]))

    def test_jobs_events_tool_uses_positional_after_limit(self):
        captured = self._capture_run()
        mcp_server.jobs_events_tool({"job_id": "J1", "after": 12, "limit": 50})
        self.assertEqual(captured["command"], "gms-rt-jobs-events")
        self.assertEqual(captured["args"], ["J1", "12", "50"])

    def test_jobs_events_tool_requires_after_before_limit(self):
        captured = self._capture_run()
        mcp_server.jobs_events_tool({"job_id": "J1", "limit": 50})
        self.assertEqual(captured["args"], ["J1"])

    def test_jobs_status_tool(self):
        captured = self._capture_run()
        mcp_server.jobs_status_tool({"job_id": "J1"})
        self.assertEqual(captured["command"], "gms-rt-jobs-status")
        self.assertEqual(captured["args"], ["J1"])

    def test_reports_list_tool_takes_no_arguments(self):
        captured = self._capture_run()
        mcp_server.reports_tool({"query": "ignored", "limit": 5})
        self.assertEqual(captured["command"], "gms-rt-reports-list")
        self.assertIsNone(captured["args"])


class JsonRpcLoopTests(unittest.TestCase):
    def _exchange(self, messages):
        """Run the server main() against a scripted stdin and collect replies."""
        stdin_lines = "\n".join(json.dumps(m) for m in messages) + "\n"
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "scripts" / "mcp_server.py")],
            input=stdin_lines,
            capture_output=True,
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
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], mcp_server.SERVER_VERSION)
        tool_names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertIn("gms_rt_run", tool_names)
        self.assertIn("gms_rt_test_start", tool_names)
        self.assertIn("gms_rt_auth_login", tool_names)
        self.assertIn("gms_rt_jobs_status", tool_names)
        self.assertIn("gms_rt_jobs_list", tool_names)
        self.assertIn("gms_rt_commands", tool_names)
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



class DocsCompactionTests(unittest.TestCase):
    def test_compact_docs_renders_lines(self):
        data = {
            "success": True,
            "apis": [
                {"method": "GET", "path": "/api/x", "description": "做某事",
                 "params": [], "skill": "gms-rt-x"},
                {"method": "POST", "path": "/api/y", "description": "带参数",
                 "params": [{"name": "device"}, {"name": "suite"}], "skill": "gms-rt-y"},
            ],
            "total": 2,
        }
        out = mcp_server._compact_docs(data)
        self.assertIsInstance(out, str)
        self.assertIn("GET /api/x | 做某事 | gms-rt-x", out)
        self.assertIn("POST /api/y | 带参数 (device,suite) | gms-rt-y", out)

    def test_compact_docs_passes_through_non_docs_shapes(self):
        self.assertEqual(mcp_server._compact_docs({"devices": []}), {"devices": []})
        self.assertEqual(mcp_server._compact_docs(None), None)

    def test_render_docs_envelope_applies_to_data(self):
        envelope = json.dumps({"ok": True, "data": {"apis": [
            {"method": "GET", "path": "/p", "description": "d", "skill": "gms-rt-p"}
        ]}})
        out = mcp_server._render_docs_envelope(envelope)
        payload = json.loads(out)
        self.assertIn("GET /p | d | gms-rt-p", payload["data"])

    def test_run_cli_renders_docs_for_system_docs_command(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "gms-remote-test.sh"
            cli.write_text('#!/bin/bash\necho \'{"ok":true,"exit_code":0,"data":{"apis":'
                           '[{"method":"GET","path":"/a","description":"甲","skill":"gms-rt-a"}]}}\'\n')
            cli.chmod(0o755)
            original = mcp_server.cli_script
            mcp_server.cli_script = lambda: cli
            try:
                text, is_error = mcp_server.run_cli("gms-rt-system-docs")
            finally:
                mcp_server.cli_script = original
        self.assertFalse(is_error)
        self.assertIn("GET /a | 甲 | gms-rt-a", text)

    def test_error_envelope_not_rendered_as_docs(self):
        envelope = json.dumps({"ok": False, "exit_code": 3,
                               "data": {"error": "auth"}})
        out = mcp_server._render_docs_envelope(envelope)
        self.assertEqual(json.loads(out)["ok"], False)

if __name__ == "__main__":
    unittest.main(verbosity=2)
