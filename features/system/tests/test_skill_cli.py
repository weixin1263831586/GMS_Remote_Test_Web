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
        if self.path == "/api/auth/status":
            self._write_json(
                200,
                {
                    "authenticated": True,
                    "auth_required": True,
                    "setup_required": False,
                    "elevated": True,
                    "user": {"username": "agent", "role": "admin"},
                },
            )
            return
        if self.path == "/api/devices/list?force_refresh=true":
            self._write_json(
                200,
                [
                    {
                        "device_id": "SERIAL-1",
                        "status": "online",
                        "protocol": "adb",
                        "locked": False,
                    }
                ],
            )
            return
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
        if self.path == "/api/test/suites":
            self._write_json(
                200,
                {
                    "success": True,
                    "count": 1,
                    "suites": [
                        {
                            "test_type": "CTS",
                            "version": "test",
                            "tools_path": "/srv/android-cts/tools",
                        }
                    ],
                },
            )
            return
        if self.path == "/api/cluster/jobs?limit=100":
            self._write_json(
                200,
                {
                    "success": True,
                    "jobs": [{"id": "job-complete", "status": "completed"}],
                },
            )
            return
        if self.path == "/api/cluster/jobs/job-complete":
            self._write_json(
                200,
                {
                    "success": True,
                    "job": {"id": "job-complete", "status": "completed"},
                },
            )
            return
        if self.path == "/api/cluster/jobs/job-failed":
            self._write_json(
                200,
                {
                    "success": True,
                    "job": {
                        "id": "job-failed",
                        "status": "failed",
                        "error": "tradefed failed",
                    },
                },
            )
            return
        if self.path == "/api/cluster/jobs/job-complete/events?after=-1&limit=500":
            self._write_json(
                200,
                {
                    "success": True,
                    "events": [{"sequence": 0, "message": "done"}],
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
        if self.path == "/api/cluster/jobs/job-complete/cancel":
            self._write_json(
                200,
                {
                    "success": True,
                    "already_terminal": True,
                    "job": {"id": "job-complete", "status": "completed"},
                },
            )
            return
        if self.path == "/api/test/stop?job_id=job-complete":
            self._write_json(200, {"success": True, "job_id": "job-complete"})
            return
        if self.path == "/api/burn/gsi":
            self._write_json(
                200,
                {
                    "success": True,
                    "results": [
                        {
                            "device": "SERIAL-1",
                            "success": True,
                            "output": "done",
                        }
                    ],
                },
            )
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
        result = self._run("gms-rt-system-capabilities", "--json")
        inventory_result = self._run("gms-rt-system-commands", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["schema_version"], 3)
        self.assertGreater(envelope["data"]["command_count"], 50)
        self.assertNotIn("commands", envelope["data"])
        self.assertEqual(
            envelope["data"]["command_inventory"]["list_command"],
            "gms-rt-system-commands",
        )
        self.assertEqual(inventory_result.returncode, 0, inventory_result.stderr)
        inventory = json.loads(inventory_result.stdout)["data"]
        commands = {
            item["name"]: item for item in inventory["commands"]
        }
        self.assertEqual(envelope["data"]["command_count"], len(commands))
        for name in (
            "gms-rt-system-capabilities",
            "gms-rt-system-command-describe",
            "gms-rt-system-commands",
            "gms-rt-system-update",
            "gms-rt-system-version",
        ):
            self.assertEqual(commands[name]["category"], "system", name)
        for name in (
            "gms-rt-capabilities",
            "gms-rt-command-describe",
            "gms-rt-commands",
            "gms-rt-update",
            "gms-rt-version",
        ):
            self.assertNotIn(name, commands)
        self.assertEqual(commands["gms-rt-system-update"]["mode"], "mutating")
        self.assertEqual(commands["gms-rt-devices-list"]["mode"], "read_only")
        self.assertEqual(commands["gms-rt-burn-firmware"]["mode"], "mutating")
        self.assertEqual(commands["gms-rt-terminal-open"]["mode"], "interactive")
        self.assertEqual(
            commands["gms-rt-adb-forward-status"]["mode"],
            "read_only",
        )
        self.assertIn("<job_id>", commands["gms-rt-jobs-wait"]["usage"])
        self.assertTrue(
            commands["gms-rt-jobs-cancel"]["requires_explicit_authorization"]
        )
        self.assertTrue(commands["gms-rt-burn-firmware"]["requires_elevation"])
        for name in (
            "gms-rt-adb-forward-status",
            "gms-rt-desktop-vnc-status",
            "gms-rt-terminal-open",
            "gms-rt-terminal-push",
            "gms-rt-test-suites-result",
            "gms-rt-usbip-connect",
            "gms-rt-usbip-disconnect",
            "gms-rt-usbip-install",
            "gms-rt-users-list",
        ):
            self.assertTrue(commands[name]["requires_elevation"], name)

    def test_command_description_and_per_invocation_server_override(self):
        described = self._run(
            "gms-rt-system-command-describe",
            "gms-rt-burn-firmware",
            "--json",
        )
        override_url = f"http://127.0.0.1:{self.server.server_port}"
        capabilities = self._run(
            "gms-rt-system-capabilities",
            "--server",
            override_url,
            "--json",
        )

        self.assertEqual(described.returncode, 0, described.stdout)
        command = json.loads(described.stdout)["data"]
        self.assertEqual(command["name"], "gms-rt-burn-firmware")
        self.assertIn("firmware_path", command["usage"])
        self.assertEqual(capabilities.returncode, 0, capabilities.stdout)
        self.assertEqual(
            json.loads(capabilities.stdout)["data"]["server"],
            override_url,
        )

    def test_removed_top_level_system_commands_are_unknown(self):
        for command in (
            "gms-rt-capabilities",
            "gms-rt-command-describe",
            "gms-rt-commands",
            "gms-rt-update",
            "gms-rt-version",
        ):
            result = self._run(command, "--json")
            self.assertEqual(result.returncode, 2, command)
            envelope = json.loads(result.stdout)
            self.assertFalse(envelope["ok"], command)
            self.assertIn("Unknown command", envelope["error"], command)

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

    def test_doctor_devices_wait_and_durable_job_commands(self):
        doctor = self._run(
            "gms-rt-system-doctor",
            "test",
            "--json",
            "--non-interactive",
        )
        devices = self._run(
            "gms-rt-devices-wait",
            "SERIAL-1",
            "--max-wait",
            "1",
            "--json",
            "--non-interactive",
        )
        jobs = self._run("gms-rt-jobs-list", "--json", "--non-interactive")
        status = self._run(
            "gms-rt-jobs-status",
            "job-complete",
            "--json",
            "--non-interactive",
        )
        events = self._run(
            "gms-rt-jobs-events",
            "job-complete",
            "--json",
            "--non-interactive",
        )
        waited = self._run(
            "gms-rt-jobs-wait",
            "job-complete",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        self.assertTrue(json.loads(doctor.stdout)["data"]["ready"])
        self.assertEqual(devices.returncode, 0, devices.stdout)
        self.assertTrue(json.loads(devices.stdout)["data"]["ready"])
        self.assertEqual(jobs.returncode, 0, jobs.stdout)
        self.assertEqual(
            json.loads(jobs.stdout)["data"]["jobs"][0]["id"],
            "job-complete",
        )
        self.assertEqual(status.returncode, 0, status.stdout)
        self.assertEqual(
            json.loads(status.stdout)["data"]["job"]["status"],
            "completed",
        )
        self.assertEqual(events.returncode, 0, events.stdout)
        self.assertEqual(
            json.loads(events.stdout)["data"]["events"][0]["sequence"],
            0,
        )
        self.assertEqual(waited.returncode, 0, waited.stdout)

    def test_job_wait_reports_terminal_failure(self):
        result = self._run(
            "gms-rt-jobs-wait",
            "job-failed",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 7, result.stdout)
        envelope = json.loads(result.stdout)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["data"]["job"]["status"], "failed")
        self.assertIn("finished with status: failed", envelope["diagnostics"])

    def test_job_cancel_and_test_stop_use_explicit_job_id(self):
        cancelled = self._run(
            "gms-rt-jobs-cancel",
            "job-complete",
            "--json",
            "--non-interactive",
        )
        stopped = self._run(
            "gms-rt-test-stop",
            "job-complete",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(cancelled.returncode, 0, cancelled.stdout)
        self.assertTrue(json.loads(cancelled.stdout)["data"]["already_terminal"])
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.assertEqual(json.loads(stopped.stdout)["data"]["job_id"], "job-complete")

    def test_gsi_burn_uses_controller_managed_runner_and_returns_json(self):
        _ApiHandler.requests.clear()
        with tempfile.NamedTemporaryFile(suffix=".img") as image:
            image.write(b"test-gsi")
            image.flush()
            result = self._run(
                "gms-rt-burn-gsi",
                image.name,
                "SERIAL",
                "--json",
                "--non-interactive",
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["data"]["success"])
        request = next(
            payload
            for path, payload in _ApiHandler.requests
            if path == "/api/burn/gsi"
        )
        self.assertEqual(request["system_img"], image.name)
        self.assertEqual(request["script_path"], "controller-managed")
        self.assertEqual(request["devices"], ["SERIAL-1"])


if __name__ == "__main__":
    unittest.main()
