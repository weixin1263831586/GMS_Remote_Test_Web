from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "skills" / "gms-remote-test" / "scripts" / "gms-remote-test.sh"


class _ApiHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/devices/list":
            self._write_json(
                403,
                {
                    "detail": {
                        "message": "Permission denied",
                        "elevation_required": True,
                    }
                },
            )
            return
        if self.path == "/api/system/health":
            self._write_json(200, {"success": True, "status": "healthy"})
            return
        if self.path == "/api/adb-forward/status":
            self._write_json(
                200,
                {
                    "success": True,
                    "connected": False,
                    "hosts": [],
                    "assignments": [],
                },
            )
            return
        self._write_json(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b""
        try:
            parsed_body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            parsed_body = {}
        self.__class__.requests.append((self.path, parsed_body))
        if self.path == "/api/config/update":
            self._write_json(409, {"detail": "Configuration is busy"})
            return
        if self.path == "/api/vpn/connect":
            self._write_json(200, {"success": False, "error": "VPN failed"})
            return
        if self.path == "/api/auth/elevate":
            self._write_json(
                200,
                {
                    "success": True,
                    "elevated": True,
                    "admin_verified": True,
                },
            )
            return
        if self.path in {"/api/adb-forward/start", "/api/adb-forward/stop"}:
            self._write_json(200, {"success": True})
            return
        self._write_json(404, {"detail": "Not found"})

    def do_DELETE(self) -> None:
        if self.path.startswith("/api/reports/delete?timestamp="):
            self._write_json(200, {"success": True, "deleted": True})
            return
        self._write_json(404, {"detail": "Not found"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


class SkillCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _run(
        self,
        *arguments: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env.update(
                {
                    "HOME": temporary,
                    "GMS_REMOTE_TEST_SERVER": (
                        f"http://127.0.0.1:{self.server.server_port}"
                    ),
                    "GMS_AUTH_COOKIE_JAR": str(Path(temporary) / "session.cookies"),
                    "NO_COLOR": "1",
                }
            )
            return subprocess.run(
                ["bash", str(HELPER), *arguments],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                input=input_text,
                check=False,
                timeout=10,
            )

    def test_capabilities_are_machine_discoverable(self):
        result = self._run("gms-rt-capabilities", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["schema_version"], 1)
        self.assertGreater(envelope["data"]["command_count"], 50)
        commands = {
            item["name"]: item for item in envelope["data"]["commands"]
        }
        self.assertEqual(commands["gms-rt-devices-list"]["mode"], "read_only")
        self.assertEqual(commands["gms-rt-burn-firmware"]["mode"], "mutating")
        self.assertEqual(commands["gms-rt-terminal-open"]["mode"], "interactive")
        self.assertEqual(
            commands["gms-rt-adb-forward-status"]["mode"],
            "read_only",
        )

    def test_json_mode_returns_structured_success(self):
        result = self._run("gms-rt-system-health", "--json", "--non-interactive")

        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["exit_code"], 0)
        self.assertEqual(envelope["data"]["status"], "healthy")

    def test_permission_error_has_stable_exit_code_and_json(self):
        result = self._run("gms-rt-devices-list", "--json", "--non-interactive")

        self.assertEqual(result.returncode, 4, result.stdout)
        envelope = json.loads(result.stdout)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["exit_code"], 4)
        self.assertTrue(envelope["data"]["detail"]["elevation_required"])
        self.assertIn("gms-rt-auth-elevate", envelope["diagnostics"])

    def test_unknown_command_is_a_json_usage_error(self):
        result = self._run("gms-rt-does-not-exist", "--json")

        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stdout)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["exit_code"], 2)

    def test_invalid_global_option_value_honors_json_contract(self):
        result = self._run(
            "gms-rt-system-health",
            "--timeout",
            "invalid",
            "--json",
        )

        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stdout)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["exit_code"], 2)
        self.assertIn("--timeout", envelope["error"])

    def test_conflict_and_business_failure_exit_codes(self):
        conflict = self._run(
            "gms-rt-config-update",
            "example",
            "value",
            "--json",
            "--non-interactive",
        )
        failed_operation = self._run(
            "gms-rt-vpn-connect",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(conflict.returncode, 5)
        self.assertEqual(json.loads(conflict.stdout)["exit_code"], 5)
        self.assertEqual(failed_operation.returncode, 7)
        self.assertEqual(json.loads(failed_operation.stdout)["exit_code"], 7)

    def test_delete_requests_keep_their_http_method(self):
        result = self._run(
            "gms-rt-reports-delete",
            "2026.07.28_12.00.00",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["data"]["deleted"])

    def test_non_interactive_login_does_not_prompt(self):
        result = self._run(
            "gms-rt-auth-login",
            "admin",
            "--non-interactive",
            "--json",
        )

        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stdout)
        self.assertFalse(envelope["ok"])
        self.assertIn("--password-stdin", envelope["diagnostics"])

    def test_elevation_reads_password_from_stdin_without_echoing_it(self):
        secret = "Admin-secret-2026!"
        result = self._run(
            "gms-rt-auth-elevate",
            "admin",
            "--password-stdin",
            "--non-interactive",
            "--json",
            input_text=f"{secret}\n",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["data"]["elevated"])
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)

    def test_adb_proxy_cli_uses_worker_aware_payloads(self):
        _ApiHandler.requests.clear()

        started = self._run(
            "gms-rt-adb-forward-start",
            "worker-source",
            "worker-target",
            "SERIAL-1",
            "SERIAL-2",
        )
        stopped = self._run(
            "gms-rt-adb-forward-stop",
            "worker-source",
            "worker-target",
        )

        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn(
            (
                "/api/adb-forward/start",
                {
                    "source_worker_id": "worker-source",
                    "target_worker_id": "worker-target",
                    "devices": ["SERIAL-1", "SERIAL-2"],
                },
            ),
            _ApiHandler.requests,
        )
        self.assertIn(
            (
                "/api/adb-forward/stop",
                {
                    "source_worker_id": "worker-source",
                    "target_worker_id": "worker-target",
                },
            ),
            _ApiHandler.requests,
        )


if __name__ == "__main__":
    unittest.main()
