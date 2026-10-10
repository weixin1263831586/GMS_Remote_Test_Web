"""Agent CLI credential lifecycle and signal semantics.

Regression tests for the CLI's credential and process-lifecycle invariants:

- ``GMS_AUTH_TOKEN_FILE`` configured but invalid (missing / loose perms /
  empty content) must FAIL CLOSED: no cookie fallback, no HTTP request at
  all, ``GMS_RT_EXIT_AUTH`` exit code;
- the Bearer header temp file (contains the token) is materialized per
  HTTP call and torn down when the request ends — sourcing the runtime
  must not leave a credential file behind;
- running the CLI directly: HUP/INT/TERM must actually terminate the
  process (129/130/143) with the EXIT trap still cleaning the credential
  temp file.

The tests execute the REAL runtime script (same pattern as
``test_cli_diagnostics.py``).
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-remote-test.sh"
SCRIPT_ARG = SCRIPT.as_posix()
BASH = os.environ.get("SHELL", "")
if Path(BASH).name.lower() not in {"bash", "bash.exe"} or not Path(BASH).is_file():
    BASH = "bash"

GMS_RT_EXIT_AUTH = 3


def _write_curl_stub(stub_dir: Path, call_log: Path) -> None:
    """A fake curl that records invocations and answers 200/empty.

    Mirrors real curl's ``-w '\\nHTTP_STATUS:%{http_code}'`` trailing
    output: the status marker must be preceded by a newline, otherwise
    ``_status_from_http_response`` parses it as ``000`` (network failure).
    """
    stub = stub_dir / "curl"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" >> {call_log.as_posix()}\n'
        'printf "ok\\nHTTP_STATUS:200\\n"\n'
    )
    stub.chmod(0o755)


class CredentialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_root = self.root / "config"
        self.state_root = self.root / "state"
        self.scratch_tmp = self.root / "tmp"
        for path in (self.config_root, self.state_root, self.root / "home", self.scratch_tmp):
            path.mkdir()
        self.scratch_tmp.chmod(0o700)
        self.token_file = self.root / "agent.token"
        self.token_file.write_text("regression-token-123")
        self.token_file.chmod(0o600)
        self.env = {
            **os.environ,
            "GMS_REMOTE_TEST_SERVER": "https://127.0.0.1:5001",
            "XDG_CONFIG_HOME": str(self.config_root),
            "XDG_STATE_HOME": str(self.state_root),
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.scratch_tmp),
            "GMS_AUTH_TOKEN_FILE": str(self.token_file),
        }

    def bearer_header_files(self) -> list[str]:
        return [p.name for p in self.scratch_tmp.glob("gms-bearer-header.*")]

    def run_snippet(self, snippet: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        command = f'source "$1"\n{snippet}\n'
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [BASH, "-c", command, "bash", SCRIPT_ARG],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    # ------------------------------------------------------------------
    # Fail-closed token validation
    # ------------------------------------------------------------------

    def _assert_invalid_token_fail_closed(self) -> None:
        call_log = self.root / "curl-calls.log"
        stub_dir = self.root / "stub"
        stub_dir.mkdir()
        _write_curl_stub(stub_dir, call_log)
        result = self.run_snippet(
            'api_call /system/health GET >/dev/null 2>&1; echo "api_rc=$?"; '
            'echo "calls=$(wc -l < "$CURL_CALL_LOG" 2>/dev/null || echo 0)"; '
            'echo "auth_args=${#CURL_AUTH_ARGS[@]}"; echo "bearer_args=${#CURL_BEARER_ARGS[@]}"',
            extra_env={
                "PATH": f"{stub_dir}{os.pathsep}{self.env.get('PATH', os.defpath)}",
                "CURL_CALL_LOG": str(call_log),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if "=" in line
        )
        # Invalid configured token ⇒ auth failure, NOT a request.
        self.assertEqual(lines["api_rc"], str(GMS_RT_EXIT_AUTH), result.stdout)
        self.assertEqual(lines["calls"], "0", "curl must never be invoked")
        self.assertEqual(lines["auth_args"], "0", "no cookie fallback allowed")
        self.assertEqual(lines["bearer_args"], "0", "no bearer header allowed")
        # The failure must not leave a credential file behind either.
        self.assertEqual(self.bearer_header_files(), [])

    def test_missing_token_file_fails_closed(self):
        self.token_file.unlink()
        self._assert_invalid_token_fail_closed()

    def test_loose_token_file_permissions_fail_closed(self):
        self.token_file.chmod(0o644)
        self._assert_invalid_token_fail_closed()

    def test_empty_token_file_fails_closed(self):
        self.token_file.write_text("   \n")
        self._assert_invalid_token_fail_closed()

    def test_unspecified_token_mode_still_uses_cookie_jar(self):
        # Sanity guard: the fail-closed rule only applies to a CONFIGURED
        # token file. Human cookie mode must keep working.
        self.env.pop("GMS_AUTH_TOKEN_FILE")
        result = self.run_snippet(
            '_refresh_tls_args; echo "rc=$?"; '
            'echo "auth_args=${#CURL_AUTH_ARGS[@]}"; echo "bearer_args=${#CURL_BEARER_ARGS[@]}"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        self.assertEqual(lines["rc"], "0")
        self.assertEqual(lines["auth_args"], "4")
        self.assertEqual(lines["bearer_args"], "0")

    def test_unsafe_discovered_profile_token_fails_closed(self):
        self.env.pop("GMS_AUTH_TOKEN_FILE")
        self.env.pop("GMS_RT_PROFILE", None)
        # An explicit Controller URL resolves to the synthetic ``direct``
        # profile, so its canonical auto-discovery candidate is direct.token.
        discovered_token = self.state_root / "gms-remote-test" / "direct.token"
        discovered_token.parent.mkdir()
        discovered_token.write_text("unsafe-discovered-token")
        discovered_token.chmod(0o644)
        call_log = self.root / "default-token-curl.log"
        stub_dir = self.root / "default-token-bin"
        stub_dir.mkdir()
        _write_curl_stub(stub_dir, call_log)
        result = self.run_snippet(
            'api_call /system/health GET >/dev/null 2>&1; echo "api_rc=$?"; '
            'echo "calls=$(wc -l < "$CURL_CALL_LOG" 2>/dev/null || echo 0)"; '
            'echo "token_file=$GMS_AUTH_TOKEN_FILE"',
            extra_env={
                "PATH": f"{stub_dir}{os.pathsep}{self.env.get('PATH', os.defpath)}",
                "CURL_CALL_LOG": str(call_log),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"token_file={discovered_token}", result.stdout)
        self.assertIn(f"api_rc={GMS_RT_EXIT_AUTH}", result.stdout)
        self.assertIn("calls=0", result.stdout)

    def test_skills_download_rejects_unsafe_name_before_request(self):
        result = self.run_snippet(
            'api_call() { echo called >> "$TMPDIR/calls"; }\n'
            'gms-rt-system-skills "../escape" >/dev/null 2>&1\n'
            'echo "request_rc=$?"\n'
            'test -f "$TMPDIR/calls" && echo called=yes || echo called=no'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request_rc=2", result.stdout)
        self.assertIn("called=no", result.stdout)

    def test_skills_download_uses_private_random_directory_and_cleans_it(self):
        download_dir = self.root / "downloads"
        download_dir.mkdir()
        result = self.run_snippet(
            f'cd {download_dir.as_posix()}\n'
            'api_call() {\n'
            '  local previous="" argument\n'
            '  for argument in "$@"; do\n'
                '    if [ "$previous" = "-o" ]; then printf "PKzip" > "$argument"; fi\n'
            '    previous="$argument"\n'
            '  done\n'
            '}\n'
            'gms-rt-system-skills safe-skill >/dev/null\n'
            'echo "request_rc=$?"\n'
            'echo "content=$(cat safe-skill-skills.zip)"\n'
            'echo "temps=$(find . -maxdepth 1 -name ".gms-skills.*" | wc -l)"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request_rc=0", result.stdout)
        self.assertIn("content=PKzip", result.stdout)
        self.assertIn("temps=0", result.stdout)

    def test_skills_download_rejects_non_zip_payload(self):
        download_dir = self.root / "invalid-download"
        download_dir.mkdir()
        result = self.run_snippet(
            f'cd {download_dir.as_posix()}\n'
            'api_call() {\n'
            '  local previous="" argument\n'
            '  for argument in "$@"; do\n'
            '    if [ "$previous" = "-o" ]; then printf "not-a-zip" > "$argument"; fi\n'
            '    previous="$argument"\n'
            '  done\n'
            '}\n'
            'gms-rt-system-skills safe-skill >/dev/null 2>&1\n'
            'echo "request_rc=$?"\n'
            'test -e safe-skill-skills.zip && echo target=yes || echo target=no\n'
            'echo "temps=$(find . -maxdepth 1 -name ".gms-skills.*" | wc -l)"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request_rc=7", result.stdout)
        self.assertIn("target=no", result.stdout)
        self.assertIn("temps=0", result.stdout)

    # ------------------------------------------------------------------
    # Bearer header temp file lifecycle
    # ------------------------------------------------------------------

    def test_sourcing_the_runtime_materializes_no_credential_file(self):
        result = self.run_snippet('echo "residual=$(ls "$TMPDIR"/gms-bearer-header.* 2>/dev/null | wc -l)"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("residual=0", result.stdout)

    def test_request_scoped_teardown_removes_bearer_header_file(self):
        result = self.run_snippet(
            '_refresh_tls_args || exit 9\n'
            'hdr_file="$_gms_bearer_header_file"\n'
            '[ -f "$hdr_file" ] || { echo "materialize=missing"; exit 9; }\n'
            'echo "hdr_mode=$(stat -c %04a "$hdr_file")"\n'
            'echo "hdr_content=$(cat "$hdr_file")"\n'
            '_gms_end_bearer_request\n'
            '[ -f "$hdr_file" ] && echo "teardown=leaked" || echo "teardown=clean"\n'
            'echo "residual=$(ls "$TMPDIR"/gms-bearer-header.* 2>/dev/null | wc -l)"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hdr_mode=0600", result.stdout)
        self.assertIn("hdr_content=Authorization: Bearer regression-token-123", result.stdout)
        self.assertIn("teardown=clean", result.stdout)
        self.assertIn("residual=0", result.stdout)

    def test_api_call_tears_down_the_header_file_after_the_request(self):
        stub_dir = self.root / "stub2"
        stub_dir.mkdir()
        call_log = self.root / "curl-calls2.log"
        _write_curl_stub(stub_dir, call_log)
        result = self.run_snippet(
            'api_call /system/health GET >/dev/null 2>&1; echo "api_rc=$?"\n'
            'echo "residual=$(ls "$TMPDIR"/gms-bearer-header.* 2>/dev/null | wc -l)"\n'
            'echo "used_bearer=$(grep -c -- "-H" "$CURL_CALL_LOG" 2>/dev/null || echo 0)"',
            extra_env={
                "PATH": f"{stub_dir}{os.pathsep}{self.env.get('PATH', os.defpath)}",
                "CURL_CALL_LOG": str(call_log),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("api_rc=0", result.stdout)
        self.assertIn("residual=0", result.stdout)
        self.assertIn("used_bearer=1", result.stdout)

    def test_burn_request_in_command_substitution_cleans_header(self):
        result = self.run_snippet(
            'curl() { printf "ok\\nHTTP_STATUS:200\\n"; }\n'
            'response=$(_post_firmware_burn_path "/tmp/fw.zip" "device-1" true)\n'
            'echo "request_rc=$?"\n'
            'echo "residual=$(ls "$TMPDIR"/gms-bearer-header.* 2>/dev/null | wc -l)"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request_rc=0", result.stdout)
        self.assertIn("residual=0", result.stdout)

    def test_streaming_request_cleans_header(self):
        result = self.run_snippet(
            'curl() { printf "stream-line\\n"; }\n'
            'gms-rt-test-logs-stream >/dev/null\n'
            'echo "request_rc=$?"\n'
            'echo "residual=$(ls "$TMPDIR"/gms-bearer-header.* 2>/dev/null | wc -l)"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request_rc=0", result.stdout)
        self.assertIn("residual=0", result.stdout)

    def test_attachment_download_cleans_header(self):
        output = self.root / "attachment.bin"
        result = self.run_snippet(
            'curl() { printf "200"; }\n'
            f'gms-rt-redmine-attachment-download artifact-1 {output.as_posix()} >/dev/null\n'
            'echo "request_rc=$?"\n'
            'echo "residual=$(ls "$TMPDIR"/gms-bearer-header.* 2>/dev/null | wc -l)"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request_rc=0", result.stdout)
        self.assertIn("residual=0", result.stdout)
        self.assertTrue(output.exists())

    def test_exec_mode_dispatch_preserves_stdin(self):
        script_text = SCRIPT.read_text(encoding="utf-8")
        patched = script_text.replace(
            '_gms_rt_dispatch "$@"',
            'IFS= read -r dispatch_input\nprintf "input=%s\\n" "$dispatch_input"',
        ).replace(
            '_gms_runtime_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)',
            f'_gms_runtime_dir={SCRIPT.parent.as_posix()}',
        )
        patched_path = self.root / "stdin-runtime.sh"
        patched_path.write_text(patched, encoding="utf-8")
        patched_path.chmod(0o755)
        result = subprocess.run(
            [BASH, patched_path.as_posix()],
            input="interactive-value\n",
            capture_output=True,
            text=True,
            env=self.env,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("input=interactive-value", result.stdout)

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _run_exec_mode_with_signal(self, sig: int, expected_rc: int) -> None:
        """Direct-execution mode: signal must terminate the process and the
        EXIT trap must remove the credential temp file.

        The dispatch call materializes the bearer header and waits on a long
        child process. The parent must terminate that child promptly instead
        of deferring its trap until the command exits naturally.
        """
        script_text = SCRIPT.read_text(encoding="utf-8")
        patched = script_text.replace(
            '_gms_rt_dispatch "$@"',
            '_refresh_tls_args || exit 9\n'
            'sleep 30 &\n'
            'printf "%s\\n" "$!" > "$GMS_TEST_CHILD_PID_FILE"\n'
            'wait "$!"',
        )
        self.assertIn("sleep 30", patched, "patch target not found")
        # The patched copy lives outside runtime/, so re-point the runtime
        # dir at the real module tree (cli/*.sh must still be loadable).
        patched = patched.replace(
            '_gms_runtime_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)',
            f'_gms_runtime_dir={SCRIPT.parent.as_posix()}',
        )
        self.assertIn(f"_gms_runtime_dir={SCRIPT.parent.as_posix()}", patched)
        patched_path = self.root / "patched-runtime.sh"
        patched_path.write_text(patched, encoding="utf-8")
        patched_path.chmod(0o755)

        proc = subprocess.Popen(
            [BASH, patched_path.as_posix()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**self.env, "GMS_TEST_CHILD_PID_FILE": str(self.root / "child.pid")},
        )
        child_file = self.root / "child.pid"
        deadline = time.monotonic() + 3
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_file.exists(), "dispatch child did not start")
        child_pid = int(child_file.read_text().strip())
        self.assertIsNone(proc.poll(), "script exited before the signal arrived")
        started = time.monotonic()
        proc.send_signal(sig)
        rc = proc.wait(timeout=5)
        self.assertLess(time.monotonic() - started, 2, "signal handling was deferred")
        self.assertEqual(rc, expected_rc, f"signal {sig} exit code")
        self.assertEqual(self.bearer_header_files(), [], "credential file leaked")
        for _ in range(50):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("dispatch child process survived the signal")

    def test_sigterm_terminates_the_cli_and_cleans_credentials(self):
        self._run_exec_mode_with_signal(signal.SIGTERM, 143)

    def test_sigint_terminates_the_cli_and_cleans_credentials(self):
        self._run_exec_mode_with_signal(signal.SIGINT, 130)

    def test_sighup_terminates_the_cli_and_cleans_credentials(self):
        self._run_exec_mode_with_signal(signal.SIGHUP, 129)


if __name__ == "__main__":
    unittest.main()
