#!/usr/bin/env python3
"""Tests for the gms-remote-test stdio MCP adapter.

The adapter is exercised end to end against a stub CLI script: build_argv
and run_cli are checked without the real Controller, and the JSON-RPC loop
is driven through the real subprocess entry point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


# In the source tree this test lives under agent/gms-remote-test/
# tests/ and imports the runtime from agent/gms-remote-test/runtime; the
# synced copy under plugins/gms-remote-test/tests/ uses scripts/.
_TEST_DIR = Path(__file__).resolve().parent
_AGENT_PKG = _TEST_DIR.parent
if (_AGENT_PKG / "runtime" / "mcp_server.py").is_file():
    RUNTIME_DIR = _AGENT_PKG / "runtime"
else:  # generated-plugin copy
    RUNTIME_DIR = _TEST_DIR.parent / "scripts"
sys.path.insert(0, str(RUNTIME_DIR))

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
                "hint": "authenticate with the Agent Service Token: enroll once with "
                "gms_rt_agent_enroll (one-shot code from the web UI), then every call "
                "authenticates via GMS_AUTH_TOKEN_FILE — no password",
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

    def test_run_cli_refreshes_token_file_from_profile_toml(self):
        """profile TOML 的 token_file 变化必须反映到子进程 env。

        launcher 在启动时固化 GMS_AUTH_TOKEN_FILE；enroll 换了 profile 的
        token 落点后，MCP 子进程若还用旧路径就会与 CLI 认证状态分裂。
        用真实 gms_agent.profile_store + 沙箱 PROFILE_ROOT 验证。
        """
        import gms_agent.profile_store as profile_store

        stub_dir = Path(self._tmp.name)
        profile_root = stub_dir / "profiles"
        profile_root.mkdir()
        profile_name = "kkagent-host-1a2b3c4d"
        toml_path = profile_root / f"{profile_name}.toml"
        old_token = stub_dir / "old.token"
        new_token = stub_dir / "new.token"
        old_token.write_text("old")
        toml_path.write_text(f'[auth]\ntoken_file = "{old_token}"\n')

        saved_root = profile_store.PROFILE_ROOT
        profile_store.PROFILE_ROOT = profile_root
        saved_environ = os.environ.copy()
        saved_cache = dict(mcp_server._TOKEN_FILE_CACHE)
        mcp_server._TOKEN_FILE_CACHE.clear()
        try:
            os.environ["GMS_RT_PROFILE"] = profile_name
            os.environ["GMS_AUTH_TOKEN_FILE"] = str(old_token)
            seen = {}

            def fake_run(argv, **kwargs):
                seen["env"] = kwargs.get("env")
                import types

                return types.SimpleNamespace(
                    stdout='{"ok":true,"data":{}}', stderr="", returncode=0
                )

            with patch.object(mcp_server.subprocess, "run", fake_run):
                mcp_server.run_cli("gms-rt-redmine-journals", ["SNAP"])
            # 同路径也显式传递当前 profile 值，使 SDK 从 B 切回 A 时不会
            # 因启动环境本来就是 A 而漏掉刷新。
            self.assertEqual(
                seen["env"].get("GMS_AUTH_TOKEN_FILE"), str(old_token)
            )

            # TOML 指向新文件：子进程 env 必须换到新路径。缓存按内容指纹
            # 失效，因此同一秒写入、等长的 old.token -> new.token 也必须
            # 被识别（这正是 stat 戳会漏掉的场景）。
            toml_path.write_text(f'[auth]\ntoken_file = "{new_token}"\n')
            with patch.object(mcp_server.subprocess, "run", fake_run):
                mcp_server.run_cli("gms-rt-redmine-journals", ["SNAP"])
            self.assertEqual(
                seen["env"].get("GMS_AUTH_TOKEN_FILE"), str(new_token)
            )
            os.environ["GMS_AUTH_TOKEN_FILE"] = str(new_token)
            toml_path.write_text(f'[auth]\ntoken_file = "{old_token}"\n')
            self.assertEqual(
                mcp_server._fresh_token_file_env()["GMS_AUTH_TOKEN_FILE"],
                str(old_token),
            )
        finally:
            profile_store.PROFILE_ROOT = saved_root
            os.environ.clear()
            os.environ.update(saved_environ)
            mcp_server._TOKEN_FILE_CACHE.clear()
            mcp_server._TOKEN_FILE_CACHE.update(saved_cache)

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

    def test_run_cli_oversized_envelope_stays_valid_json(self):
        """An envelope larger than MAX_OUTPUT_BYTES must still parse as
        JSON after adapter trimming — the old head/tail text cut produced
        invalid JSON with is_error=False (silently corrupted data)."""
        import subprocess as _subprocess

        def fake_run(argv, **kwargs):
            payload = json.dumps({
                "ok": True, "exit_code": 0,
                "data": {"logs": "x" * (mcp_server.MAX_OUTPUT_BYTES + 100)},
            })
            return _subprocess.CompletedProcess(
                argv, 0, stdout=payload, stderr="",
            )

        original = mcp_server.subprocess.run
        mcp_server.subprocess.run = fake_run
        try:
            text, is_error = mcp_server.run_cli("gms-rt-jobs-events", ["J1"])
        finally:
            mcp_server.subprocess.run = original
        self.assertFalse(is_error)
        payload = json.loads(text)  # must not raise
        self.assertEqual(payload["ok"], True)
        self.assertIn("truncated", payload["data"]["logs"])

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

    def test_run_tool_denial_points_devices_shell_to_typed_tools(self):
        # The generic runner must keep denying the raw command (interactive
        # by catalog), but the message now routes agents to the two typed
        # paths instead of a dead end.
        catalog = {
            "gms-rt-devices-shell": {
                "name": "gms-rt-devices-shell",
                "mode": "interactive",
                "requires_explicit_authorization": True,
                "agent_safe_unattended": False,
            }
        }
        mcp_server._CATALOG_CACHE["commands"] = catalog
        text, is_error = mcp_server.run_tool({"command": "gms-rt-devices-shell"})
        self.assertTrue(is_error)
        self.assertIn("not agent-safe", text)
        self.assertIn("gms_rt_shell", text)
        self.assertIn("gms_rt_shell_exec", text)

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

        def fake_run(command, args=None, stdin_text=None, timeout=None,
                     env_extra=None):
            captured["command"] = command
            captured["args"] = args
            captured["stdin_text"] = stdin_text
            captured["env_extra"] = env_extra
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
        # worker_id is optional; explicit worker avoids the cluster-resolve
        # call in unit tests (no controller available).
        mcp_server.test_start_tool({
            "device": "RK3572", "type": "CTS", "module": "m1",
            "wait": True, "max_wait": 300, "worker_id": "w1",
        })
        self.assertEqual(captured["command"], "gms-rt-test-start")
        self.assertEqual(
            captured["args"],
            ["RK3572", "CTS", "m1", "--worker", "w1", "--wait", "--max-wait", "300"],
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

    def test_context_tool_uses_system_selfcheck(self):
        captured = self._capture_run()
        mcp_server.context_tool({})
        self.assertEqual(captured["command"], "gms-rt-system-selfcheck")

    def test_device_console_tool_builds_exact_cli_arguments(self):
        captured = self._capture_run()
        text, is_error = mcp_server.device_console_tool(
            {"port_key": "usb-FTDI-port0", "tail": 500, "date": "20260911"}
        )
        self.assertFalse(is_error, text)
        self.assertEqual(captured["command"], "gms-rt-devices-console")
        self.assertEqual(
            captured["args"],
            ["usb-FTDI-port0", "--tail", "500", "--date", "20260911"],
        )

    def test_device_console_tool_rejects_invalid_tail_and_date(self):
        text, is_error = mcp_server.device_console_tool({"tail": "many"})
        self.assertTrue(is_error)
        self.assertIn("tail", text)
        text, is_error = mcp_server.device_console_tool({"date": "2026-09-11"})
        self.assertTrue(is_error)
        self.assertIn("YYYYMMDD", text)

    def test_device_info_tool_accepts_multiple_devices(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.device_info_tool({"devices": ["D1", "D2"]})
        self.assertFalse(is_error)
        self.assertEqual(captured["command"], "gms-rt-devices-info")
        self.assertEqual(captured["args"], ["D1", "D2"])

    def test_device_wait_tool_builds_bounded_wait(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.device_wait_tool(
            {
                "devices": "D1",
                "state": "fastboot",
                "interval": 5,
                "max_wait": 600,
                "timeout": 620,
            }
        )
        self.assertFalse(is_error)
        self.assertEqual(captured["command"], "gms-rt-devices-wait")
        self.assertEqual(
            captured["args"],
            ["D1", "--state", "fastboot", "--interval", "5", "--max-wait", "600"],
        )

    def test_jobs_cancel_tool_requires_and_passes_job_id(self):
        text, is_error = mcp_server.jobs_cancel_tool({})
        self.assertTrue(is_error)
        self.assertIn("job_id", text)
        captured = self._capture_run()
        _text, is_error = mcp_server.jobs_cancel_tool({"job_id": "J1"})
        self.assertFalse(is_error)
        self.assertEqual(captured["command"], "gms-rt-jobs-cancel")
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
            [sys.executable, str(RUNTIME_DIR / "mcp_server.py")],
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

    def test_typed_core_tools_are_advertised_with_cli_equivalents(self):
        by_name = {tool["name"]: tool for tool in mcp_server.tools()}
        expected = {
            "gms_rt_context": "gms-rt-system-selfcheck",
            "gms_rt_device_console": "gms-rt-devices-console",
            "gms_rt_device_info": "gms-rt-devices-info",
            "gms_rt_device_wait": "gms-rt-devices-wait",
            "gms_rt_jobs_cancel": "gms-rt-jobs-cancel",
        }
        for name, cli_name in expected.items():
            self.assertIn(name, by_name)
            self.assertIn(cli_name, by_name[name]["description"])
            self.assertNotIn("-", name)
            self.assertIn("annotations", by_name[name])

    def test_toolset_filter_reduces_catalog_and_is_enforced_on_call(self):
        with patch.dict(os.environ, {"GMS_MCP_TOOLSETS": "core"}):
            names = {tool["name"] for tool in mcp_server.tools()}
            self.assertIn("gms_rt_context", names)
            self.assertIn("gms_rt_devices", names)
            self.assertNotIn("gms_rt_test_start", names)
            self.assertNotIn("gms_rt_jobs_cancel", names)
            replies = []
            with patch.object(
                mcp_server,
                "response",
                side_effect=lambda *args, **kwargs: replies.append((args, kwargs)),
            ):
                mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "gms_rt_jobs_cancel",
                            "arguments": {"job_id": "J1"},
                        },
                    }
                )
            self.assertEqual(replies[0][1]["error"]["code"], -32601)

    def test_json_object_result_has_structured_content_and_text_fallback(self):
        replies = []
        with (
            patch.dict(
                mcp_server._TOOL_HANDLERS,
                {"gms_rt_context": lambda _args: ('{"ok":true,"data":{}}', False)},
            ),
            patch.object(
                mcp_server,
                "response",
                side_effect=lambda *args, **kwargs: replies.append((args, kwargs)),
            ),
        ):
            mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "gms_rt_context", "arguments": {}},
                }
            )
        payload = replies[0][0][1]
        self.assertEqual(payload["structuredContent"], {"ok": True, "data": {}})
        self.assertEqual(payload["content"][0]["type"], "text")



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


class JobsCompactionTests(unittest.TestCase):
    """One-line-per-job rendering for jobs-list / single-job trimming."""

    def _sample_job(self, job_id="job-1", status="completed"):
        return {
            "id": job_id,
            "status": status,
            "created_at": "2026-09-05T07:00:47Z",
            "finished_at": "2026-09-05T07:08:53Z",
            "error": None,
            "request": {
                "devices": ["RK3562GMS7"],
                "test_module": "CtsDeqpTestCases",
                "test_case": "dEQP-VK.x#y",
            },
            "attempt": {"status": "completed", "error": None, "result": {}},
            "leases": [{"id": "claim-1", "status": "released"}],
        }

    def test_compact_jobs_list_renders_one_line_per_job(self):
        data = {"jobs": [self._sample_job(), self._sample_job("job-2", "failed")]}
        out = mcp_server._compact_jobs_list(data)
        lines = out.splitlines()
        self.assertEqual(lines[0], mcp_server._compact_jobs_list.__doc__ and lines[0])
        self.assertIn("# 2 jobs | columns: job_id | status | attempt | devices", lines[0])
        self.assertIn("job-1 | completed | completed | RK3562GMS7 | CtsDeqpTestCases", lines[1])
        self.assertIn("job-2 | failed", lines[2])
        self.assertNotIn("leases", out)

    def test_compact_jobs_list_passes_through_other_shapes(self):
        self.assertEqual(mcp_server._compact_jobs_list({"devices": []}), {"devices": []})
        self.assertEqual(mcp_server._compact_jobs_list(None), None)

    def test_compact_job_single_trims_nested_payload(self):
        data = {
            "id": "job-1",
            "status": "failed",
            "created_at": "2026-09-05T09:35:25Z",
            "request": {"devices": ["RK3562GMS7"], "test_module": "M", "test_case": "C"},
            "attempt": {
                "status": "failed",
                "error": "device fencing claim expired",
                "result": {"exit_code": 0, "work_dir": "/tmp/w"},
            },
            "state_version": 4,
            "recovery_count": 0,
        }
        out = mcp_server._compact_job_single(data)
        self.assertEqual(out["id"], "job-1")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["error"], "device fencing claim expired")
        self.assertEqual(out["attempt_exit_code"], 0)
        self.assertNotIn("state_version", out)
        self.assertNotIn("attempt", out)

    def test_compact_job_single_passes_through_other_shapes(self):
        self.assertEqual(mcp_server._compact_job_single({"foo": 1}), {"foo": 1})

    def test_render_data_envelope_is_generic(self):
        envelope = json.dumps({"ok": True, "data": {"jobs": [self._sample_job()]}})
        out = mcp_server._render_data_envelope(envelope, mcp_server._compact_jobs_list)
        payload = json.loads(out)
        self.assertIn("job-1 | completed", payload["data"])


class AuthElevateAndBurnToolTests(unittest.TestCase):
    def setUp(self):
        _reset_catalog_cache()
        self.addCleanup(_reset_catalog_cache)

    def _capture_run(self):
        captured = {}

        def fake_run(command, args=None, stdin_text=None, timeout=None,
                     env_extra=None):
            captured["command"] = command
            captured["args"] = args
            captured["stdin_text"] = stdin_text
            captured["timeout"] = timeout
            return '{"ok":true,"exit_code":0,"data":{}}', False

        original = mcp_server.run_cli
        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        return captured

    def test_auth_elevate_requires_username_and_secret(self):
        text, is_error = mcp_server.auth_elevate_tool({"password_stdin": "x"})
        self.assertTrue(is_error)
        self.assertIn("username", text)
        text, is_error = mcp_server.auth_elevate_tool({"username": "gms"})
        self.assertTrue(is_error)
        self.assertIn("password_stdin", text)

    def test_auth_elevate_forwards_secret_on_stdin_only(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.auth_elevate_tool(
            {"username": "gms", "password_stdin": "adm1n"}
        )
        self.assertFalse(is_error)
        self.assertEqual(captured["command"], "gms-rt-auth-elevate")
        self.assertEqual(captured["args"], ["gms", "--password-stdin"])
        self.assertEqual(captured["stdin_text"], "adm1n\n")
        self.assertNotIn("adm1n", json.dumps(captured["args"]))

    def test_burn_firmware_requires_path_and_device(self):
        text, is_error = mcp_server.burn_firmware_tool({"device": "D1"})
        self.assertTrue(is_error)
        self.assertIn("firmware_path", text)
        text, is_error = mcp_server.burn_firmware_tool({"firmware_path": "/a/img"})
        self.assertTrue(is_error)
        self.assertIn("device", text)

    def test_burn_firmware_defaults_wipe_true_and_timeout_1800(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.burn_firmware_tool(
            {
                "firmware_path": "/a/update.img",
                "device": "RK3562GMS7",
                "approval_token": "tok",
                "wait": True,
            }
        )
        self.assertFalse(is_error)
        self.assertEqual(captured["command"], "gms-rt-burn-firmware")
        self.assertEqual(
            captured["args"],
            ["/a/update.img", "RK3562GMS7", "true", "--approval-token", "tok"],
        )
        self.assertEqual(captured["timeout"], 1800)

    def test_burn_firmware_wipe_false_and_wait_online(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.burn_firmware_tool({
            "firmware_path": "/a/update.img", "device": "D1,D2",
            "wipe_data": False, "wait_online": True, "wait_online_max": 900,
            "approval_token": "tok", "wait": True,
        })
        self.assertFalse(is_error)
        self.assertEqual(
            captured["args"],
            [
                "/a/update.img", "D1,D2", "false",
                "--approval-token", "tok",
                "--wait-online", "--wait-online=900",
            ],
        )

    def test_burn_firmware_requires_approval_token(self):
        text, is_error = mcp_server.burn_firmware_tool(
            {"firmware_path": "/a/update.img", "device": "RK3562GMS7"}
        )
        self.assertTrue(is_error)
        self.assertIn("approval", text)

    def test_burn_firmware_rejects_bad_wait_online_max(self):
        text, is_error = mcp_server.burn_firmware_tool({
            "firmware_path": "/a/img", "device": "D1",
            "wait_online": True, "wait_online_max": "soon",
            "approval_token": "tok", "wait": True,
        })
        self.assertTrue(is_error)
        self.assertIn("wait_online_max", text)


class AsyncBurnOperationTests(unittest.TestCase):
    """wait=false 后台烧录：start → operation_id → status。"""

    def setUp(self):
        _reset_catalog_cache()
        self.addCleanup(_reset_catalog_cache)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._ops_patcher = patch.dict(
            os.environ, {"GMS_BURN_OPS_DIR": self._tmp.name}
        )
        self._ops_patcher.start()
        self.addCleanup(self._ops_patcher.stop)

    def test_async_start_returns_operation_id_immediately(self):
        # wait=false 必须走异步路径：不再调用 run_cli（同步等待），
        # 而是后台启动并立即返回 operation_id。用 stub CLI 保证确定性。
        stub_dir = Path(self._tmp.name) / "bin-async"
        stub_dir.mkdir(exist_ok=True)
        stub = stub_dir / "fake-cli-async"
        stub.write_text("#!/bin/sh\necho '{\"ok\":true,\"exit_code\":0}'\n")
        stub.chmod(0o755)
        original_argv = mcp_server.build_argv
        mcp_server.build_argv = lambda command, args: [str(stub), *args]
        self.addCleanup(lambda: setattr(mcp_server, "build_argv", original_argv))

        text, is_error = mcp_server.burn_firmware_tool({
            "firmware_path": "/a/img", "device": "D1",
            "approval_token": "tok", "wait": False,
        })
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "running")
        self.assertRegex(payload["operation_id"], r"^burn-[0-9a-f-]+$")
        self.assertIn("gms_rt_burn_status", payload["hint"])

    def test_start_and_poll_full_lifecycle(self):
        import time as _time

        # stub "CLI"：写 envelope 到 stdout 后退出。
        stub_dir = Path(self._tmp.name) / "bin"
        stub_dir.mkdir(exist_ok=True)
        stub = stub_dir / "fake-cli"
        stub.write_text("#!/bin/sh\necho '{\"ok\":true,\"exit_code\":0,\"data\":{\"burned\":true}}'\n")
        stub.chmod(0o755)

        original_argv = mcp_server.build_argv
        mcp_server.build_argv = (
            lambda command, args: [str(stub), *args]
        )
        self.addCleanup(lambda: setattr(mcp_server, "build_argv", original_argv))

        text, is_error = mcp_server.start_burn_operation(
            "gms-rt-burn-firmware", ["/a/img", "D1", "true"]
        )
        self.assertFalse(is_error)
        payload = json.loads(text)
        operation_id = payload["operation_id"]
        self.assertEqual(payload["status"], "running")
        # operation_id 只包含安全字符，防止路径穿越。
        self.assertRegex(operation_id, r"^burn-[0-9a-f-]+$")

        # 进程存活期间 → running（带 recent_output）。
        status_text, status_error = mcp_server.burn_status_tool(
            {"operation_id": operation_id}
        )
        self.assertFalse(status_error)
        status = json.loads(status_text)
        self.assertIn(status["status"], {"running", "finished"})

        # 进程退出后 → finished，并提取 envelope。
        _time.sleep(0.3)
        deadline = _time.time() + 5
        final = None
        while _time.time() < deadline:
            status_text, status_error = mcp_server.burn_status_tool(
                {"operation_id": operation_id}
            )
            self.assertFalse(status_error)
            status = json.loads(status_text)
            if status["status"] == "finished":
                final = status
                break
            _time.sleep(0.1)
        self.assertIsNotNone(final, status_text)
        self.assertEqual(final["result"]["data"]["burned"], True)
        self.assertEqual(final["exit_code"], 0)

    def test_burn_status_rejects_path_traversal(self):
        for evil in ("../escape", "a/b", "", "burn-../../etc"):
            text, is_error = mcp_server.burn_status_tool({"operation_id": evil})
            self.assertTrue(is_error, evil)
            self.assertIn("operation_id", text)

    def test_burn_status_unknown_operation(self):
        text, is_error = mcp_server.burn_status_tool(
            {"operation_id": "burn-00000000-000000-abcdef"}
        )
        self.assertTrue(is_error)
        self.assertIn("not found", text)

    def test_burn_status_rejects_non_burn_prefix(self):
        _text, is_error = mcp_server.burn_status_tool({"operation_id": "xyz-1"})
        self.assertTrue(is_error)



class ShellToolGateTests(unittest.TestCase):
    """gms_rt_shell read-only command gate (added 2026-09-05)."""

    def test_gate_allows_readonly_commands(self):
        for cmd in (
            "getprop ro.build.fingerprint",
            "dumpsys window",
            "logcat -d -b events -v threadtime",
            "settings get secure user_setup_complete",
            "ls /system/etc",
            "pidof com.google.android.setupwizard",
            # ONE restricted pipe — readonly head, filter tail.
            "dumpsys window | grep focus",
            "getprop | grep build",
            "ps -A | wc -l",
            "logcat -d -v threadtime | tail -50",
        ):
            allowed, reason = mcp_server._validate_shell_command(cmd)
            self.assertTrue(allowed, f"{cmd!r} should be allowed: {reason}")

    def test_gate_pipe_restrictions(self):
        """Pipe is single, tail-filtered, and fully validated."""
        for cmd in (
            "getprop | grep x | wc -l",      # more than one pipe
            "getprop | sh",                  # tail not a filter binary
            "getprop | xargs echo",          # tail not a filter binary
            "getprop | grep x; reboot",      # forbidden char in segment
            "getprop |",                     # empty tail segment
            "| grep x",                      # empty head segment
            "getprop || grep x",             # '||' is chaining, not a pipe
            "input tap 1 2 | grep x",        # head must pass the full gate
            "getprop | grep sh -c x",        # tail itself fully validated
        ):
            allowed, _reason = mcp_server._validate_shell_command(cmd)
            self.assertFalse(allowed, f"{cmd!r} should be denied")

    def test_gate_denies_chaining_and_mutation(self):
        for cmd in (
            "logcat -b all",
            "settings put secure x y",
            "wm density 480",
            "dumpsys battery unplug",
            "sh -c x",
            "input tap 1 2",
            "getprop; reboot",
        ):
            allowed, _reason = mcp_server._validate_shell_command(cmd)
            self.assertFalse(allowed, f"{cmd!r} should be denied")

    def test_shell_tool_requires_device_and_command(self):
        out, is_err = mcp_server.shell_tool({"command": "getprop"})
        self.assertTrue(is_err)
        self.assertIn("device", out)
        out, is_err = mcp_server.shell_tool({"device": "D1"})
        self.assertTrue(is_err)
        self.assertIn("command", out)

    def test_gate_denies_mutating_variants_found_by_audit(self):
        """Token-exact flag matching and the narrow dumpsys blacklist
        let real mutating commands through the read-only gate."""
        for cmd in (
            "dumpsys battery set level 1",           # 'set' not in old blacklist
            "logcat -d --clear",                     # long option form of -c
            "logcat -d -f/data/local/tmp/audit.log", # attached -f value
            "logcat -d --file=/data/local/tmp/a.log",
            "dmesg -c",                              # clears kernel ring buffer
            "dmesg -C",
            "dumpsys battery plug",                  # 'plug' simulates charging
        ):
            allowed, _reason = mcp_server._validate_shell_command(cmd)
            self.assertFalse(allowed, f"{cmd!r} must be denied")

    def test_gate_still_allows_readonly_logcat_and_dmesg(self):
        for cmd in (
            "logcat -d -v time",
            "logcat -T 10 -d",
            "dmesg",
            "dmesg -T",
            "dmesg -r",
        ):
            allowed, reason = mcp_server._validate_shell_command(cmd)
            self.assertTrue(allowed, f"{cmd!r} should be allowed: {reason}")


class LogcatToolTests(unittest.TestCase):
    """gms_rt_logcat typed tool (added v0.7.0)."""

    def _capture_run(self):
        captured = {}

        def fake_run(command, args=None, stdin_text=None, timeout=None,
                     env_extra=None):
            captured["command"] = command
            captured["args"] = args
            captured["stdin_text"] = stdin_text
            captured["timeout"] = timeout
            return '{"ok":true,"exit_code":0,"data":{}}', False

        original = mcp_server.run_cli
        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        return captured

    def test_logcat_requires_device(self):
        out, is_err = mcp_server.logcat_tool({})
        self.assertTrue(is_err)
        self.assertIn("device", out)

    def test_logcat_rejects_bad_device_id(self):
        out, is_err = mcp_server.logcat_tool({"device": "D1;reboot"})
        self.assertTrue(is_err)
        self.assertIn("denied", out)

    def test_logcat_adds_dump_flag_by_default(self):
        captured = self._capture_run()
        _out, is_err = mcp_server.logcat_tool({"device": "RK3562GMS7"})
        self.assertFalse(is_err)
        self.assertEqual(captured["command"], "gms-rt-devices-logcat")
        self.assertEqual(captured["args"], ["RK3562GMS7", "-d"])
        self.assertEqual(captured["timeout"], 180)

    def test_logcat_keeps_existing_dump_flag(self):
        captured = self._capture_run()
        _out, is_err = mcp_server.logcat_tool(
            {"device": "D1", "args": ["-t", "500"]}
        )
        self.assertFalse(is_err)
        self.assertEqual(captured["args"], ["D1", "-t", "500"])

    def test_logcat_accepts_string_args(self):
        captured = self._capture_run()
        _out, is_err = mcp_server.logcat_tool(
            {"device": "D1", "args": "-b crash -s ActivityManager"}
        )
        self.assertFalse(is_err)
        self.assertEqual(
            captured["args"],
            ["D1", "-d", "-b", "crash", "-s", "ActivityManager"],
        )

    def test_logcat_denies_file_flag(self):
        for bad in ("-f", "--file=x"):
            out, is_err = mcp_server.logcat_tool({"device": "D1", "args": [bad]})
            self.assertTrue(is_err, bad)
            self.assertIn("denied", out)

    def test_logcat_clear_true_is_denied(self):
        # Clearing the log buffer destroys CTS/GTS diagnostic
        # evidence, so the MCP tool refuses clear=true in every spelling;
        # clearing is a human CLI step (logcat -c).
        for value in (True, "true"):
            out, is_err = mcp_server.logcat_tool(
                {"device": "D1", "args": ["-b", "crash"], "clear": value}
            )
            self.assertTrue(is_err, value)
            self.assertIn("denied", out)

    def test_logcat_clear_false_by_default(self):
        captured = self._capture_run()
        _out, is_err = mcp_server.logcat_tool({"device": "D1"})
        self.assertFalse(is_err)
        self.assertEqual(captured["args"], ["D1", "-d"])

    def test_logcat_raw_clear_flag_is_denied(self):
        # Raw -c/--clear in args is denied with a pointer to the human CLI.
        for bad in ("-c", "--clear"):
            out, is_err = mcp_server.logcat_tool({"device": "D1", "args": [bad]})
            self.assertTrue(is_err, bad)
            self.assertIn("denied", out)

    def test_logcat_denies_metacharacters(self):
        for bad in ("a;b", "x|y", "$(id)", "`id`", "p'q", 'p"q'):
            out, is_err = mcp_server.logcat_tool(
                {"device": "D1", "args": [bad]}
            )
            self.assertTrue(is_err, bad)
            self.assertIn("denied", out)

    def test_logcat_rejects_wrong_args_type_and_too_many(self):
        out, is_err = mcp_server.logcat_tool({"device": "D1", "args": {"x": 1}})
        self.assertTrue(is_err)
        out, is_err = mcp_server.logcat_tool(
            {"device": "D1", "args": [f"a{i}" for i in range(20)]}
        )
        self.assertTrue(is_err)
        self.assertIn("too many", out)

    def test_logcat_timeout_is_clamped_and_validated(self):
        captured = self._capture_run()
        _out, is_err = mcp_server.logcat_tool(
            {"device": "D1", "timeout": 9999}
        )
        self.assertFalse(is_err)
        self.assertEqual(captured["timeout"], 600)
        out, is_err = mcp_server.logcat_tool({"device": "D1", "timeout": "x"})
        self.assertTrue(is_err)
        self.assertIn("timeout", out)

    def test_logcat_registered_in_tools_and_handlers(self):
        names = {tool["name"] for tool in mcp_server.tools()}
        self.assertIn("gms_rt_logcat", names)
        self.assertIn("gms_rt_logcat", mcp_server._TOOL_HANDLERS)


class ShellExecToolTests(unittest.TestCase):
    """gms_rt_shell_exec approval-token gate (replaces authorized=true, v0.9.0)."""

    def _capture_run(self):
        captured = {}

        def fake_run(command, args=None, stdin_text=None, timeout=None,
                     env_extra=None):
            captured["command"] = command
            captured["args"] = args
            captured["timeout"] = timeout
            return '{"ok":true,"exit_code":0,"data":{}}', False

        original = mcp_server.run_cli
        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        return captured

    def test_requires_device_and_command(self):
        out, is_err = mcp_server.shell_exec_tool({})
        self.assertTrue(is_err)
        self.assertIn("device", out)
        out, is_err = mcp_server.shell_exec_tool({"device": "D1"})
        self.assertTrue(is_err)
        self.assertIn("command", out)

    def test_denies_without_approval_token(self):
        out, is_err = mcp_server.shell_exec_tool({
            "device": "D1", "command": "am broadcast -a X", "authorized": True,
        })
        self.assertTrue(is_err)
        self.assertIn("denied", out)
        self.assertIn("approval", out)

    def test_approval_token_forwards_to_devices_shell(self):
        captured = self._capture_run()
        _out, is_err = mcp_server.shell_exec_tool({
            "device": "RK3562GMS7",
            "command": "settings put global wifi_on 1",
            "approval_token": "tok123",
        })
        self.assertFalse(is_err)
        self.assertEqual(captured["command"], "gms-rt-devices-shell")
        self.assertEqual(
            captured["args"],
            [
                "RK3562GMS7", "--approval-token", "tok123",
                "settings put global wifi_on 1",
            ],
        )
        self.assertEqual(captured["timeout"], 120)

    def test_rejects_bad_device_id_and_oversized_command(self):
        out, is_err = mcp_server.shell_exec_tool({
            "device": "D1;reboot", "command": "ls", "approval_token": "t",
        })
        self.assertTrue(is_err)
        self.assertIn("denied", out)
        out, is_err = mcp_server.shell_exec_tool({
            "device": "D1", "command": "x" * 2001, "approval_token": "t",
        })
        self.assertTrue(is_err)
        self.assertIn("exceeds", out)

    def test_timeout_clamped_and_validated(self):
        captured = self._capture_run()
        _out, is_err = mcp_server.shell_exec_tool({
            "device": "D1", "command": "ls", "approval_token": "t", "timeout": 9999,
        })
        self.assertFalse(is_err)
        self.assertEqual(captured["timeout"], 600)
        out, is_err = mcp_server.shell_exec_tool({
            "device": "D1", "command": "ls", "approval_token": "t", "timeout": "x",
        })
        self.assertTrue(is_err)
        self.assertIn("timeout", out)

    def test_registered_in_tools_and_handlers(self):
        names = {tool["name"] for tool in mcp_server.tools()}
        self.assertIn("gms_rt_shell_exec", names)
        self.assertIn("gms_rt_shell_exec", mcp_server._TOOL_HANDLERS)


class ApkToolTests(unittest.TestCase):
    """gms_rt_apk_* typed tools bridge the suite-module decompilation flow."""

    def setUp(self):
        _reset_catalog_cache()
        self.addCleanup(_reset_catalog_cache)

    def _capture_run(self):
        captured = {}

        def fake_run(command, args=None, stdin_text=None, timeout=None,
                     env_extra=None):
            captured["command"] = command
            captured["args"] = args
            return '{"ok":true,"exit_code":0,"data":{}}', False

        original = mcp_server.run_cli
        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        return captured

    def test_resolve_requires_query(self):
        text, is_error = mcp_server.apk_resolve_tool({})
        self.assertTrue(is_error)
        self.assertIn("query", text)

    def test_resolve_builds_flag_args(self):
        captured = self._capture_run()
        mcp_server.apk_resolve_tool({
            "query": "CtsCamera", "suite_types": "cts,gts", "prefer": "jar",
        })
        self.assertEqual(captured["command"], "gms-rt-apk-resolve")
        self.assertEqual(
            captured["args"],
            ["CtsCamera", "--types", "cts,gts", "--prefer", "jar"],
        )

    def test_analyze_defaults_to_background_polling(self):
        captured = self._capture_run()
        mcp_server.apk_analyze_tool({"query": "CtsCamera"})
        self.assertEqual(captured["command"], "gms-rt-apk-analyze")
        self.assertEqual(captured["args"], ["CtsCamera"])

    def test_analyze_wait_mode_passes_max_wait(self):
        captured = self._capture_run()
        mcp_server.apk_analyze_tool({
            "query": "CtsCamera", "wait": True, "max_wait": 600,
        })
        self.assertEqual(captured["args"], ["CtsCamera", "--wait", "--max-wait", "600"])

    def test_status_manifest_require_task_id(self):
        for tool in (mcp_server.apk_status_tool, mcp_server.apk_manifest_tool):
            text, is_error = tool({})
            self.assertTrue(is_error)
            self.assertIn("task_id", text)
        captured = self._capture_run()
        mcp_server.apk_status_tool({"task_id": "T1"})
        self.assertEqual(captured["args"], ["T1"])

    def test_search_requires_query_and_clamps_limit(self):
        text, is_error = mcp_server.apk_search_tool({"task_id": "T1"})
        self.assertTrue(is_error)
        self.assertIn("query", text)
        captured = self._capture_run()
        mcp_server.apk_search_tool({"task_id": "T1", "query": "Permission", "limit": 999})
        self.assertEqual(captured["args"], ["T1", "Permission", "--limit", "50"])
        mcp_server.apk_search_tool({
            "task_id": "T1", "query": "onCreate", "mode": "symbol",
            "path": "com/example", "line": 42,
        })
        self.assertEqual(
            captured["args"],
            ["T1", "onCreate", "--mode", "symbol", "--path", "com/example", "--line", "42"],
        )

    def test_search_rejects_unknown_mode(self):
        text, is_error = mcp_server.apk_search_tool({
            "task_id": "T1", "query": "x", "mode": "bogus",
        })
        self.assertTrue(is_error)
        self.assertIn("mode", text)

    def test_manifest_permissions_flag(self):
        captured = self._capture_run()
        mcp_server.apk_manifest_tool({"task_id": "T1"})
        self.assertEqual(captured["args"], ["T1"])
        mcp_server.apk_manifest_tool({"task_id": "T1", "permissions": True})
        self.assertEqual(captured["args"], ["T1", "--permissions"])

    def test_source_view_routes_to_source_read(self):
        captured = self._capture_run()
        mcp_server.apk_source_tool({
            "task_id": "T1", "path": "com/example/A.java", "view": True,
        })
        self.assertEqual(captured["command"], "gms-rt-apk-source-read")
        self.assertEqual(captured["args"], ["T1", "com/example/A.java"])
        text, is_error = mcp_server.apk_source_tool({"task_id": "T1", "view": True})
        self.assertTrue(is_error)
        self.assertIn("path", text)
        mcp_server.apk_source_tool({"task_id": "T1", "path": "com/example"})
        self.assertEqual(captured["command"], "gms-rt-apk-source")
        self.assertEqual(captured["args"], ["T1", "com/example"])

    def test_registered_in_tools_and_handlers(self):
        names = {tool["name"] for tool in mcp_server.tools()}
        for name in (
            "gms_rt_apk_resolve", "gms_rt_apk_analyze", "gms_rt_apk_status",
            "gms_rt_apk_manifest", "gms_rt_apk_search", "gms_rt_apk_source",
        ):
            self.assertIn(name, names)
            self.assertIn(name, mcp_server._TOOL_HANDLERS)


class RedmineEvidenceToolTests(unittest.TestCase):
    """Redmine/apk/sdk typed tools wiring."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original_cli = mcp_server.cli_script
        self.cli_path = Path(self._tmp.name) / "gms-remote-test.sh"
        self.cli_path.write_text('#!/bin/bash\necho \'{"ok":true,"exit_code":0,"data":{}}\'\nexit 0\n')
        self.cli_path.chmod(0o755)
        mcp_server.cli_script = lambda: self.cli_path

    def tearDown(self):
        mcp_server.cli_script = self._original_cli

    def _capture_run(self) -> dict:
        captured = {}

        def fake_run(command, args=None, **_kwargs):
            captured["command"] = command
            captured["args"] = args
            return "{}", False

        original = mcp_server.run_cli
        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        return captured

    def test_fetch_requires_issue(self):
        text, is_error = mcp_server.redmine_issue_fetch_tool({})
        self.assertTrue(is_error)
        self.assertIn("issue", text)

    def test_fetch_maps_arguments(self):
        captured = self._capture_run()
        mcp_server.redmine_issue_fetch_tool({
            "issue": "648526", "download": "all", "wait": True, "max_wait": 60,
        })
        self.assertEqual(captured["command"], "gms-rt-redmine-issue-fetch")
        self.assertEqual(
            captured["args"],
            ["648526", "--download", "all", "--wait", "--max-wait", "60"],
        )

    def test_fetch_maps_dry_run(self):
        captured = self._capture_run()
        mcp_server.redmine_issue_fetch_tool({"issue": "648526", "dry_run": True})
        self.assertEqual(
            captured["args"], ["648526", "--dry-run"]
        )

    def test_fetch_rejects_bad_download(self):
        _, is_error = mcp_server.redmine_issue_fetch_tool({
            "issue": "1", "download": "everything",
        })
        self.assertTrue(is_error)

    def test_journals_pagination_args(self):
        captured = self._capture_run()
        mcp_server.redmine_journals_tool({
            "snapshot_id": "SNAP", "limit": 100, "cursor": "100",
        })
        self.assertEqual(
            captured["args"], ["SNAP", "--limit", "100", "--cursor", "100"],
        )

    def test_artifact_read_window_args(self):
        captured = self._capture_run()
        mcp_server.redmine_artifact_read_tool({
            "artifact_id": "ART", "offset": 4096, "limit": 8192,
        })
        self.assertEqual(
            captured["args"], ["ART", "--offset", "4096", "--limit", "8192"],
        )

    def test_image_tool_returns_mcp_image_content(self):
        captured = self._capture_run()

        def fake_run(command, args=None, **_kwargs):
            captured["command"] = command
            captured["args"] = args
            return json.dumps({
                "ok": True,
                "data": {
                    "artifact_id": "ART",
                    "mime_type": "image/png",
                    "base64": "aVZCUg==",
                    "sha256": "ff" * 32,
                    "size_bytes": 95,
                    "scaled": False,
                },
            }), False

        mcp_server.run_cli = fake_run
        result = mcp_server.redmine_image_tool({"artifact_id": "ART"})
        self.assertIsInstance(result, mcp_server.ToolContent)
        types = [item["type"] for item in result.items]
        self.assertEqual(types, ["text", "image"])
        image = result.items[1]
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(image["data"], "aVZCUg==")
        self.assertFalse(result.is_error)
        self.assertEqual(captured["command"], "gms-rt-redmine-artifact-image")

    def test_image_tool_requires_artifact(self):
        result = mcp_server.redmine_image_tool({})
        self.assertTrue(result.is_error)

    def test_sdk_read_requires_result_id_only(self):
        # read 只接受自包含 opaque result_id。
        text, is_error = mcp_server.sdk_read_tool({})
        self.assertTrue(is_error)
        self.assertIn("result_id", text)
        captured = self._capture_run()
        mcp_server.sdk_read_tool({
            "result_id": "src1_AAAA_BBBB", "offset": 10, "limit": 50,
        })
        self.assertEqual(captured["args"], ["src1_AAAA_BBBB", "--offset", "10", "--limit", "50"])
        self.assertEqual(captured["command"], "gms-rt-sdk-read")

    def test_sdk_search_and_read_args(self):
        captured = self._capture_run()
        mcp_server.sdk_search_tool({
            "source": "android14", "revision": "main", "query": "testMethod",
        })
        self.assertEqual(
            captured["args"],
            ["--source", "android14", "--revision", "main", "--query", "testMethod"],
        )
        mcp_server.sdk_read_tool({
            "result_id": "src_x", "source": "android14", "path": "a.java",
            "commit": "c" * 40, "offset": 10, "limit": 100,
        })
        self.assertEqual(
            captured["args"][-4:], ["--offset", "10", "--limit", "100"],
        )

    def test_apk_attachment_and_source_tools(self):
        captured = self._capture_run()
        mcp_server.apk_analyze_attachment_tool({
            "snapshot_id": "SNAP", "artifact_id": "ART",
        })
        self.assertEqual(
            captured["args"], ["SNAP", "ART"],
        )
        mcp_server.apk_source_search_tool({
            "task_id": "T1", "query": "AssertionError", "path": "com/example",
        })
        self.assertEqual(
            captured["args"],
            ["T1", "AssertionError", "--mode", "content", "--path", "com/example"],
        )
        mcp_server.apk_source_read_tool({
            "task_id": "T1", "path": "a.java", "offset": 5, "limit": 50,
        })
        self.assertEqual(
            captured["args"], ["T1", "a.java", "--offset", "5", "--limit", "50"],
        )

    def test_registered_in_tools_and_handlers(self):
        names = {tool["name"] for tool in mcp_server.tools()}
        for name in (
            "gms_rt_redmine_issue_fetch", "gms_rt_redmine_issue",
            "gms_rt_redmine_triage",
            "gms_rt_redmine_history_search",
            "gms_rt_redmine_journals", "gms_rt_redmine_attachments",
            "gms_rt_redmine_artifact_search", "gms_rt_redmine_artifact_read",
            "gms_rt_redmine_image", "gms_rt_apk_analyze_attachment",
            "gms_rt_apk_source_search", "gms_rt_apk_source_read",
            "gms_rt_sdk_sources", "gms_rt_sdk_search", "gms_rt_sdk_read",
        ):
            self.assertIn(name, names)
            self.assertIn(name, mcp_server._TOOL_HANDLERS)

    def test_triage_tool_maps_arguments_to_cli(self):
        captured: dict[str, Any] = {}

        def fake_run_cli(command: str, args: list[str] | None = None):
            captured["command"] = command
            captured["args"] = list(args or [])
            return "{}", False

        with patch.object(mcp_server, "run_cli", fake_run_cli):
            mcp_server.redmine_triage_tool({})
            self.assertEqual(captured, {"command": "gms-rt-redmine-triage", "args": []})

            mcp_server.redmine_triage_tool({
                "stale_days": 5, "list_limit": 50, "refresh": True,
            })
            self.assertEqual(captured["args"], [
                "--stale-days", "5", "--list-limit", "50", "--refresh",
            ])

            # 越界参数被 clamp 到 schema 上限，不透传原始输入。
            mcp_server.redmine_triage_tool({"stale_days": 999})
            self.assertEqual(captured["args"], ["--stale-days", "30"])

    def test_history_search_tool_maps_arguments_to_cli(self):
        captured: dict[str, Any] = {}

        def fake_run_cli(command: str, args: list[str] | None = None):
            captured["command"] = command
            captured["args"] = list(args or [])
            return "{}", False

        with patch.object(mcp_server, "run_cli", fake_run_cli):
            self.assertIn(
                "gms_rt_redmine_history_search", mcp_server._TOOL_HANDLERS,
            )
            # 空 query 是参数错误，不发起 CLI 调用。
            mcp_server.redmine_history_search_tool({})
            self.assertNotIn("command", captured)
            mcp_server.redmine_history_search_tool({"q": "  "})
            self.assertNotIn("command", captured)

            mcp_server.redmine_history_search_tool({
                "q": "RK3562 Android16 SSI", "limit": 10,
                "exclude_issue_id": 650761, "resolved_only": True,
            })
            self.assertEqual(captured, {
                "command": "gms-rt-redmine-history-search",
                "args": ["RK3562 Android16 SSI", "--limit", "10",
                         "--exclude-issue-id", "650761", "--resolved-only"],
            })

    def test_schemas_use_closed_objects(self):
        for tool in mcp_server.tools():
            if tool["name"].startswith(("gms_rt_redmine_", "gms_rt_sdk_")) or tool["name"] in (
                "gms_rt_apk_source_search", "gms_rt_apk_source_read",
                "gms_rt_apk_analyze_attachment",
            ):
                self.assertEqual(
                    tool["inputSchema"].get("additionalProperties"), False,
                    tool["name"],
                )


class ServiceTokenBoundaryTests(unittest.TestCase):
    """The tool-catalog boundary is service-token mode.

    mcp_launcher.py must FORCE GMS_AGENT_AUTH_MODE (a plain setdefault let
    an ambient auth-mode variable from the parent shell re-enable the
    password/elevation tools) and stamp GMS_AGENT_PROCESS=1 — the server
    treats EITHER signal as sufficient, so forging one alone cannot widen
    the catalog on a launcher-launched agent.
    """

    @staticmethod
    def _service_token_mode(env: dict[str, str]) -> str:
        # Strip inherited GMS_AGENT* vars so the host's own profile can
        # never leak into the assertion.
        clean = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GMS_AGENT")
        }
        clean.update(env)
        code = (
            f"import sys; sys.path.insert(0, {str(RUNTIME_DIR)!r}); "
            "import mcp_server; print(int(mcp_server._SERVICE_TOKEN_MODE))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=clean,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_forced_auth_mode_enables_service_token_mode(self):
        self.assertEqual(
            self._service_token_mode({"GMS_AGENT_AUTH_MODE": "service-token"}), "1"
        )

    def test_launcher_process_stamp_alone_enables_service_token_mode(self):
        self.assertEqual(self._service_token_mode({"GMS_AGENT_PROCESS": "1"}), "1")

    def test_no_agent_signals_stays_human_mode(self):
        self.assertEqual(self._service_token_mode({}), "0")

    def test_launcher_forces_auth_mode_and_stamps_process(self):
        source = (RUNTIME_DIR / "mcp_launcher.py").read_text(encoding="utf-8")
        self.assertNotIn('setdefault("GMS_AGENT_AUTH_MODE"', source)
        self.assertIn('os.environ["GMS_AGENT_AUTH_MODE"] = "service-token"', source)
        self.assertIn('os.environ["GMS_AGENT_PROCESS"] = "1"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
