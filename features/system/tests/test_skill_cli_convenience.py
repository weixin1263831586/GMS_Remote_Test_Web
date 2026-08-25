"""CLI convenience features: short suite names, device prefixes, blocking waits."""

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

SUITE_TOOLS_PATH = "/srv/android-cts/tools"


class _ApiHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []

    def _write_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        self.__class__.requests.append((self.path, parsed))
        return parsed

    def do_GET(self) -> None:
        self._record()
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
                    },
                    {
                        "device_id": "AUXILIARY-2",
                        "status": "online",
                        "protocol": "adb",
                        "locked": False,
                    },
                ],
            )
            return
        if self.path == "/api/test/suites":
            self._write_json(
                200,
                {
                    "success": True,
                    "count": 2,
                    "suites": [
                        {
                            "test_type": "CTS",
                            "version": "android-test-suite",
                            "tools_path": SUITE_TOOLS_PATH,
                            "binary": "cts-tradefed",
                        },
                        {
                            "test_type": "CTS",
                            "version": "android-test-suite",
                            "tools_path": SUITE_TOOLS_PATH,
                            "binary": "cts-v-host-tradefed",
                        },
                    ],
                },
            )
            return
        if self.path == "/api/cluster/jobs/job-complete":
            self._write_json(
                200,
                {"success": True, "job": {"id": "job-complete", "status": "completed"}},
            )
            return
        self._write_json(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        parsed = self._record()
        if self.path == "/api/devices/reboot":
            self._write_json(
                200,
                {
                    "success": True,
                    "data": {
                        "summary": {"total": 1, "success": 1, "failed": 0},
                        "results": [
                            {"device": "SERIAL-1", "success": True, "output": "rebooted"}
                        ],
                    },
                },
            )
            return
        if self.path == "/api/test/parse-args":
            params = parsed.get("params", [])
            suite = next(
                (item for item in params if isinstance(item, str) and "/" in item), ""
            )
            self._write_json(
                200,
                {
                    "success": True,
                    "device": params[0] if params else "",
                    "test_type": params[1] if len(params) > 1 else "",
                    "test_module": "",
                    "test_case": "",
                    "test_suite": suite,
                    "retry_dir": "",
                    "warnings": [],
                },
            )
            return
        if self.path == "/api/test/start":
            self._write_json(
                200,
                {"success": True, "data": {"cluster_job_id": "job-complete"}},
            )
            return
        if self.path == "/api/test/suites/result":
            self._write_json(
                200,
                {
                    "success": True,
                    "count": 1,
                    "results": [],
                    "raw_output": "Session 1 passed",
                    "cached": False,
                },
            )
            return
        if self.path.startswith("/api/burn/firmware"):
            self._write_json(
                200,
                {
                    "success": True,
                    "data": {
                        "summary": {"total": 1, "success": 1, "failed": 0},
                        "results": [{"device": "SERIAL-1", "success": True}],
                    },
                },
            )
            return
        self._write_json(404, {"detail": "Not found"})

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @classmethod
    def request_paths(cls) -> list[str]:
        return [path for path, _payload in cls.requests]


class SkillCliConvenienceTests(unittest.TestCase):
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
        env_extra: dict[str, str] | None = None,
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
            if env_extra:
                env.update(env_extra)
            return subprocess.run(
                ["bash", str(HELPER), *arguments],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

    def test_device_prefix_resolves_to_full_serial(self):
        _ApiHandler.requests.clear()
        result = self._run(
            "gms-rt-devices-reboot",
            "SERIAL",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        request = next(
            payload
            for path, payload in _ApiHandler.requests
            if path == "/api/devices/reboot"
        )
        self.assertEqual(request["devices"], ["SERIAL-1"])

    def test_mixed_exact_device_and_prefix_are_both_preserved(self):
        _ApiHandler.requests.clear()
        result = self._run(
            "gms-rt-devices-reboot",
            '["SERIAL-1","AUXILIARY"]',
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        envelope = json.loads(result.stdout)
        self.assertNotIn("diagnostics", envelope)
        request = next(
            payload
            for path, payload in _ApiHandler.requests
            if path == "/api/devices/reboot"
        )
        self.assertEqual(request["devices"], ["SERIAL-1", "AUXILIARY-2"])

    def test_test_suites_result_accepts_short_suite_name(self):
        _ApiHandler.requests.clear()
        result = self._run(
            "gms-rt-test-suites-result",
            "android-test-suite",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        request = next(
            payload
            for path, payload in _ApiHandler.requests
            if path == "/api/test/suites/result"
        )
        self.assertEqual(request["suite_path"], SUITE_TOOLS_PATH)

    def test_test_start_resolves_short_suite_name_and_waits_for_job(self):
        _ApiHandler.requests.clear()
        result = self._run(
            "gms-rt-test-start",
            "SERIAL",
            "CTS",
            "android-test-suite",
            "--wait",
            "--max-wait",
            "30",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        parse_request = next(
            payload
            for path, payload in _ApiHandler.requests
            if path == "/api/test/parse-args"
        )
        self.assertIn(SUITE_TOOLS_PATH, parse_request["params"])
        start_request = next(
            payload
            for path, payload in _ApiHandler.requests
            if path == "/api/test/start"
        )
        self.assertEqual(start_request["devices"], ["SERIAL-1"])
        self.assertIn("/api/cluster/jobs/job-complete", _ApiHandler.request_paths())

    def test_burn_firmware_wait_online_polls_device_state(self):
        _ApiHandler.requests.clear()
        with tempfile.NamedTemporaryFile(suffix=".zip") as archive:
            archive.write(b"firmware")
            archive.flush()
            result = self._run(
                "gms-rt-burn-firmware",
                archive.name,
                "SERIAL",
                "true",
                "--wait-online=30",
                "--json",
                "--non-interactive",
                env_extra={"GMS_BURN_UPLOAD_MODE": "http"},
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "/api/devices/list?force_refresh=true",
            _ApiHandler.request_paths(),
        )
        self.assertIn(
            "/api/burn/firmware?devices=SERIAL-1",
            _ApiHandler.request_paths(),
        )


if __name__ == "__main__":
    unittest.main()
