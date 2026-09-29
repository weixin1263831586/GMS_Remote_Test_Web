"""Agent-facing CLI diagnostics UX.

Covers the friction points from the 2026-09-22 GRF/VBA triage:

- global ``--help``/``-h`` must never leak into a command as a positional
  argument (``gms-rt-devices-snapshot --help`` used to report
  ``{"device": "--help"}``);
- unknown commands get git-style closest-match hints (mirroring the MCP
  adapter's suggestions);
- ``gms-rt-devices-diag`` is the agent-safe read-only front door over the
  same typed-readonly allowlist as MCP ``gms_rt_shell``, with a local audit
  trail;
- ``gms-rt-devices-snapshot`` returns a real diagnostic bundle whose failed
  probes carry per-probe reasons instead of silent nulls;
- the machine catalog advertises ``output_shape`` so callers do not have to
  guess each command's ``data`` envelope.

The tests execute the REAL runtime script (same pattern as
``TestEnrollTomlOnly.test_shell_enroll_rejects_environment_profile_controller_mismatch``).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-remote-test.sh"
SCRIPT_ARG = SCRIPT.as_posix()
BASH = os.environ.get("SHELL", "")
if Path(BASH).name.lower() not in {"bash", "bash.exe"} or not Path(BASH).is_file():
    BASH = "bash"

# Controller URL on loopback keeps _is_test_host() true so the local adb
# branch (stubbed via PATH) is exercised without SSH or a real device.
BASE_ENV = {
    "GMS_REMOTE_TEST_SERVER": "https://127.0.0.1:5001",
}


class CliDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        config_root = self.root / "config"
        state_root = self.root / "state"
        config_root.mkdir()
        state_root.mkdir()
        self.env = {
            **os.environ,
            **BASE_ENV,
            "XDG_CONFIG_HOME": str(config_root),
            "XDG_STATE_HOME": str(state_root),
            "HOME": str(self.root / "home"),
        }
        (self.root / "home").mkdir()
        self.state_root = state_root

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        """Execute the real script through its dispatcher (production path)."""
        return subprocess.run(
            [BASH, SCRIPT_ARG, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )

    def run_snippet(self, snippet: str) -> subprocess.CompletedProcess:
        """Source the script and run a snippet (for overriding inner helpers)."""
        command = f'source "$1"\n{snippet}\n'
        return subprocess.run(
            [BASH, "-c", command, "bash", SCRIPT_ARG],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )

    # ------------------------------------------------------------------
    # Global --help / -h interception
    # ------------------------------------------------------------------

    def test_help_flag_prints_usage_instead_of_positional_leak(self):
        result = self.run_cli("gms-rt-devices-snapshot", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: gms-rt-devices-snapshot <device_id>", result.stdout)
        self.assertNotIn('"--help"', result.stdout)

    def test_short_help_flag_prints_usage(self):
        result = self.run_cli("gms-rt-devices-info", "-h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: gms-rt-devices-info", result.stdout)

    def test_help_flag_respects_json_output_contract(self):
        result = self.run_cli("gms-rt-devices-wait", "--json", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "gms-rt-devices-wait")
        self.assertIn("<devices>", payload["usage"])

    def test_help_with_extra_arguments_still_reaches_the_command(self):
        # Only a sole --help/-h is intercepted; anything else must keep the
        # historical behavior so per-command parsers stay in charge.
        result = self.run_cli("gms-rt-devices-wait", "DEV", "--state", "bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--state requires online, fastboot, or any", result.stderr)

    # ------------------------------------------------------------------
    # Closest-match hints for unknown commands
    # ------------------------------------------------------------------

    def test_unknown_command_suggests_closest_match(self):
        result = self.run_cli("gms-rt-devices-shel")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown command: gms-rt-devices-shel", result.stderr)
        self.assertIn("gms-rt-devices-shell", result.stderr)
        self.assertIn("Closest matches:", result.stderr)

    def test_unknown_command_without_match_points_at_help(self):
        result = self.run_cli("gms-rt-xyzzyq")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown command: gms-rt-xyzzyq", result.stderr)
        self.assertIn("gms-rt-system-help", result.stderr)

    # ------------------------------------------------------------------
    # gms-rt-devices-diag read-only front door
    # ------------------------------------------------------------------

    def _service_token_env(self):
        token_file = self.root / "agent.token"
        token_file.write_text("test-token\n", encoding="utf-8")
        token_file.chmod(0o600)
        self.env["GMS_AUTH_TOKEN_FILE"] = str(token_file)

    def _stub_adb(self) -> Path:
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "adb"
        stub.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "devices" ]; then echo "DIAGDEV\tdevice product:box"; exit 0; fi\n'
            'echo "stub-adb: $*"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}"
        return bin_dir

    def test_diag_requires_device_and_command(self):
        result = self.run_cli("gms-rt-devices-diag")
        self.assertEqual(result.returncode, 2)
        self.assertIn("设备ID必填", result.stderr)
        result = self.run_cli("gms-rt-devices-diag", "DIAGDEV")
        self.assertEqual(result.returncode, 2)
        self.assertIn("缺少诊断命令", result.stderr)

    def test_diag_allowlisted_command_runs_and_is_audited(self):
        self._service_token_env()
        self._stub_adb()
        result = self.run_cli(
            "gms-rt-devices-diag", "DIAGDEV", "getprop ro.build.fingerprint"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub-adb: -s DIAGDEV shell getprop ro.build.fingerprint", result.stdout)
        audit = self.state_root / "gms-remote-test" / "diag-audit.log"
        lines = audit.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("\tDIAGDEV\tgetprop ro.build.fingerprint\tstart", lines[0])
        self.assertIn("\texit=0", lines[1])

    def test_diag_denies_non_allowlisted_command_without_approval_token(self):
        self._service_token_env()
        self._stub_adb()
        result = self.run_cli("gms-rt-devices-diag", "DIAGDEV", "reboot")
        self.assertEqual(result.returncode, 4)
        self.assertIn("一次性审批令牌", result.stderr)
        self.assertEqual(result.stdout, "")
        audit = self.state_root / "gms-remote-test" / "diag-audit.log"
        self.assertIn("exit=4", audit.read_text(encoding="utf-8"))

    def test_diag_denies_shell_metacharacters(self):
        self._service_token_env()
        self._stub_adb()
        # First token must be allowlisted so the gate reaches the
        # metacharacter check (a bad first token is rejected earlier with
        # the approval-token guidance instead).
        result = self.run_cli(
            "gms-rt-devices-diag", "DIAGDEV", "getprop ro.build.fingerprint; reboot"
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("元字符", result.stderr)

    def test_diag_allows_single_restricted_pipe_like_mcp(self):
        # MCP gms_rt_shell 放行 "getprop | grep build" 一类的单管道过滤；
        # CLI typed-readonly 门禁必须接受同一形态（工具契约一致）。
        self._service_token_env()
        self._stub_adb()
        result = self.run_cli("gms-rt-devices-diag", "DIAGDEV", "getprop | grep build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub-adb: -s DIAGDEV shell getprop | grep build", result.stdout)

    def test_diag_rejects_non_filter_pipe_tail(self):
        self._service_token_env()
        self._stub_adb()
        result = self.run_cli("gms-rt-devices-diag", "DIAGDEV", "getprop | reboot")
        self.assertEqual(result.returncode, 4)
        self.assertIn("grep/wc/head/tail", result.stderr)

    def test_diag_rejects_double_pipe(self):
        self._service_token_env()
        self._stub_adb()
        result = self.run_cli(
            "gms-rt-devices-diag", "DIAGDEV", "getprop | grep a | wc -l"
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("管道", result.stderr)

    # ------------------------------------------------------------------
    # Snapshot diagnostic bundle
    # ------------------------------------------------------------------

    def test_snapshot_reports_collected_total_and_per_probe_errors(self):
        self.env["GMS_RT_OUTPUT"] = "json"
        snippet = """
gms-rt-devices-shell() {
  case "$2" in
    "getprop ro.build.fingerprint") echo "brand/model:/target:1.0/1:eng" ;;
    "cat /proc/cmdline") echo "console=ttyS0,115200 androidboot.vba=1" ;;
    *) return 7 ;;
  esac
}
gms-rt-devices-snapshot DIAGDEV
"""
        result = self.run_snippet(snippet)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["collected"], 2)
        self.assertEqual(payload["total"], 9)
        self.assertEqual(
            payload["fingerprint"], "brand/model:/target:1.0/1:eng"
        )
        self.assertEqual(
            payload["kernel_cmdline"], "console=ttyS0,115200 androidboot.vba=1"
        )
        self.assertIsNone(payload["focused_activity"])
        self.assertIsNone(payload["boot_props"])
        self.assertEqual(len(payload["errors"]), 7)
        failed = {item["probe"]: item for item in payload["errors"]}
        self.assertEqual(failed["dumpsys battery"]["exit_code"], 7)
        self.assertEqual(
            failed["dumpsys battery"]["error"], "command failed (exit 7)"
        )
        self.assertNotIn("getprop ro.build.fingerprint", failed)

    def test_snapshot_failure_reason_carries_stderr_excerpt(self):
        self.env["GMS_RT_OUTPUT"] = "json"
        snippet = """
gms-rt-devices-shell() {
  echo "error: device 'DIAGDEV' offline" >&2
  return 1
}
gms-rt-devices-snapshot DIAGDEV
"""
        result = self.run_snippet(snippet)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["collected"], 0)
        offline = [
            item
            for item in payload["errors"]
            if "offline" in item["error"]
        ]
        self.assertTrue(offline, payload["errors"])
        self.assertEqual(offline[0]["exit_code"], 1)

    # ------------------------------------------------------------------
    # Machine catalog: new command + output_shape
    # ------------------------------------------------------------------

    def test_catalog_lists_diag_as_agent_safe_with_output_shape(self):
        result = self.run_cli("gms-rt-system-commands")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        commands = {item["name"]: item for item in payload["commands"]}
        diag = commands["gms-rt-devices-diag"]
        self.assertEqual(diag["mode"], "read_only")
        self.assertTrue(diag["agent_safe_unattended"])
        self.assertFalse(diag["requires_explicit_authorization"])
        self.assertEqual(diag["output_shape"], "data-object")
        self.assertEqual(commands["gms-rt-devices-list"]["output_shape"], "data-array")
        self.assertEqual(commands["gms-rt-devices-console"]["output_shape"], "data-mixed")

    def test_catalog_is_available_without_controller_or_profile(self):
        env = dict(self.env)
        env.pop("GMS_REMOTE_TEST_SERVER", None)
        env.pop("GMS_RT_PROFILE", None)
        env.pop("GMS_AGENT_PROFILE", None)
        env["GMS_PORT"] = "65534"
        result = subprocess.run(
            [
                BASH,
                SCRIPT_ARG,
                "gms-rt-system-commands",
                "--json",
                "--non-interactive",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertGreater(len(payload["data"]["commands"]), 0)

    # ------------------------------------------------------------------
    # Profile resolution resilience (sys2206 triage: a full /tmp used to
    # zero the profile context via the heredoc temp file and masquerade
    # as "无法解析 Agent profile")
    # ------------------------------------------------------------------

    def test_profile_resolution_does_not_need_writable_tmpdir(self):
        self.env["TMPDIR"] = str(self.root / "no-such-tmpdir")
        result = self.run_snippet('printf "mode=%s\\n" "$_gms_profile_mode"')
        self.assertEqual(result.returncode, 0, result.stderr)
        # No profile store entries + explicit GMS_REMOTE_TEST_SERVER in the
        # sandbox => the resolver must answer "human", never the generic
        # resolution failure.
        self.assertEqual(result.stdout.strip(), "mode=human")
        self.assertNotIn("无法解析", result.stderr)
        self.assertNotIn("解析器", result.stderr)

    def test_approval_create_help_documents_human_session_path(self):
        # The dispatcher answers a sole --help with the catalog usage line;
        # the detailed human-session guidance lives in the command's own
        # -h branch, reached when --help appears among other arguments or
        # when the function is called directly (sourced mode).
        result = self.run_cli("gms-rt-approval-create", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: gms-rt-approval-create", result.stdout)
        detailed = self.run_snippet("gms-rt-approval-create --help")
        self.assertEqual(detailed.returncode, 0, detailed.stderr)
        self.assertIn("Usage: gms-rt-approval-create", detailed.stdout)
        self.assertIn("GMS_RT_HUMAN_SESSION=1 gms-rt-auth-login", detailed.stdout)
        catalog = self.run_cli("gms-rt-system-command-describe",
                               "gms-rt-approval-create")
        self.assertEqual(catalog.returncode, 0, catalog.stderr)
        self.assertIn("GMS_RT_HUMAN_SESSION", catalog.stdout)


if __name__ == "__main__":
    unittest.main()
