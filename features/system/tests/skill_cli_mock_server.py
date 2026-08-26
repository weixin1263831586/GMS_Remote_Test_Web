"""Shared mock API server for skill CLI tests.

Serves the minimal endpoints exercised by ``gms-remote-test.sh`` so tests can
run the CLI as a real subprocess against a loopback server.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler


class ApiHandler(BaseHTTPRequestHandler):
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
