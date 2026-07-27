from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bootstrap.application import create_app
from features.auth import auth_service
from features.cluster import ClusterRepository, ClusterService
from features.cluster import api as cluster_api
from features.system import security_audit_logger


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.original_audit_path = security_audit_logger.log_path
        self.original_audit_lock_path = security_audit_logger.lock_path
        security_audit_logger.log_path = str(
            Path(self.tmp.name) / "security_audit.json"
        )
        security_audit_logger.lock_path = (
            f"{security_audit_logger.log_path}.lock"
        )
        security_audit_logger._head_hash = None
        self.cluster_config_path = Path(self.tmp.name) / "cluster.json"
        self.cluster_config_path.write_text("{}", encoding="utf-8")
        self.tokens_path = Path(self.tmp.name) / "worker_tokens.json"
        self.tokens_path.write_text(
            json.dumps(
                {"worker_tokens": {"worker-local": "worker-token-for-security-tests-000001"}}
            ),
            encoding="utf-8",
        )
        self.environment = patch.dict(
            "os.environ",
            {
                "GMS_AUTH_REQUIRED": "true",
                "GMS_ENV": "production",
                "GMS_SECURE_COOKIES": "true",
                "CORS_ORIGINS": "",
                "GMS_SECRET_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
                "GMS_AUDIT_HMAC_KEY": "audit-key-for-security-boundary-tests-0001",
                "GMS_METRICS_TOKEN": "metrics-token-for-security-tests-000001",
                "GMS_AUTOMATION_WEBHOOK_TOKEN": "webhook-token-for-security-tests-00001",
                "GMS_AUTOMATION_OWNER_ID": "service-automation",
                "GMS_CLUSTER_CONFIG": str(self.cluster_config_path),
                "GMS_WORKER_TOKENS_FILE": str(self.tokens_path),
                "GMS_ALLOWED_ORIGINS": "https://testserver",
                "TRUSTED_HOSTS": "testserver",
            },
        )
        self.environment.start()
        self.client = TestClient(create_app(), base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized
        security_audit_logger.log_path = self.original_audit_path
        security_audit_logger.lock_path = self.original_audit_lock_path
        security_audit_logger._head_hash = None
        self.tmp.cleanup()

    def _setup_admin(self):
        return self.client.post(
            "/api/auth/setup",
            headers={"Origin": "https://testserver"},
            json={"username": "admin", "password": "strongpass1"},
        )

    def test_anonymous_access_is_default_deny_with_explicit_public_routes(self):
        client_identity = self.client.get("/api/users/current")
        status = self.client.get("/api/auth/status")
        health = self.client.get("/api/system/health")
        installer = self.client.get("/api/system/skills/install.sh")
        skill_archive = self.client.get("/api/system/skills")

        self.assertEqual(client_identity.status_code, 200)
        self.assertIsNone(client_identity.json()["user"])
        self.assertNotIn("password", client_identity.text.lower())
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["auth_required"])
        self.assertEqual(health.status_code, 200)
        self.assertEqual(installer.status_code, 200)
        self.assertIn(
            "https://testserver/api/system/skills?skill_name=gms-remote-test",
            installer.text,
        )
        self.assertNotIn("__GMS_REMOTE_TEST_SERVER__", installer.text)
        self.assertEqual(skill_archive.status_code, 200)
        self.assertEqual(skill_archive.headers["content-type"], "application/zip")

    def test_setup_and_authenticated_requests_obey_same_origin_policy(self):
        blocked_setup = self.client.post(
            "/api/auth/setup",
            headers={"Origin": "https://attacker.example"},
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(blocked_setup.status_code, 403)

        setup = self._setup_admin()
        self.assertEqual(setup.status_code, 200)
        self.assertEqual(self.client.get("/api/users/current").status_code, 200)

        blocked_write = self.client.post(
            "/api/auth/elevate",
            headers={"Origin": "https://attacker.example"},
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(blocked_write.status_code, 403)
        self.assertEqual(blocked_write.json()["error"], "Request origin is not allowed")

    def test_default_cors_does_not_reflect_untrusted_origin(self):
        response = self.client.options(
            "/api/auth/status",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_untrusted_host_header_is_rejected(self):
        response = self.client.get(
            "/api/auth/status",
            headers={"Host": "attacker.example"},
        )

        self.assertEqual(response.status_code, 400)

    def test_security_headers_are_applied_to_success_and_denial(self):
        denied = self.client.get("/api/users/current")
        allowed = self.client.get("/api/auth/status")

        for response in (denied, allowed):
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(response.headers["x-frame-options"], "SAMEORIGIN")
            self.assertEqual(response.headers["referrer-policy"], "same-origin")
            self.assertTrue(response.headers.get("x-request-id"))

    def test_openapi_declares_default_session_and_service_security(self):
        schema = self.client.app.openapi()

        self.assertEqual(
            schema["components"]["securitySchemes"]["SessionCookie"],
            {
                "type": "apiKey",
                "in": "cookie",
                "name": "gms_session",
                "description": "Authenticated browser session cookie.",
            },
        )
        self.assertEqual(schema["security"], [{"SessionCookie": []}])
        self.assertEqual(schema["paths"]["/api/auth/login"]["post"]["security"], [])
        self.assertEqual(
            schema["paths"]["/api/cluster/workers/register"]["post"]["security"],
            [{"ServiceBearer": []}],
        )

    def test_production_cookie_is_secure_by_default(self):
        self.client.close()
        with patch.dict("os.environ", {"GMS_SECURE_COOKIES": "true"}):
            self.client = TestClient(create_app(), base_url="https://testserver")
            response = self.client.post(
                "/api/auth/setup",
                headers={"Origin": "https://testserver"},
                json={"username": "admin", "password": "strongpass1"},
            )

        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("secure", cookie)
        self.assertNotIn("max-age=", cookie)

    def test_production_rejects_disabled_authentication_or_secure_cookies(self):
        with (
            patch.dict("os.environ", {"GMS_AUTH_REQUIRED": "false"}),
            self.assertRaisesRegex(RuntimeError, "GMS_AUTH_REQUIRED"),
        ):
            create_app()
        with (
            patch.dict("os.environ", {"GMS_SECURE_COOKIES": "false"}),
            self.assertRaisesRegex(RuntimeError, "GMS_SECURE_COOKIES"),
        ):
            create_app()

    def test_production_rejects_missing_service_tokens(self):
        with (
            patch.dict("os.environ", {"GMS_METRICS_TOKEN": ""}),
            self.assertRaisesRegex(RuntimeError, "GMS_METRICS_TOKEN"),
        ):
            create_app()
        with (
            patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": "/nonexistent/worker_tokens.json"},
            ),
            self.assertRaisesRegex(RuntimeError, "worker tokens are required"),
        ):
            create_app()

    def test_websocket_and_novnc_reject_anonymous_handshakes(self):
        for path in ("/api/system/websocket/browser", "/websockify"):
            with (
                self.assertRaises(WebSocketDisconnect) as raised,
                self.client.websocket_connect(
                    path,
                    headers={"Origin": "https://testserver"},
                ),
            ):
                pass
            self.assertEqual(raised.exception.code, 4401)

    def test_authenticated_websocket_accepts_same_origin_and_rejects_cross_origin(self):
        self.assertEqual(self._setup_admin().status_code, 200)

        with self.client.websocket_connect(
            "/api/system/websocket/browser",
            headers={
                "Origin": "https://testserver",
                "Cookie": f"gms_session={self.client.cookies.get('gms_session')}",
            },
        ) as websocket:
            websocket.send_json({"type": "ping"})
            self.assertEqual(websocket.receive_json()["type"], "pong")

        with (
            self.assertRaises(WebSocketDisconnect) as raised,
            self.client.websocket_connect(
                "/api/system/websocket/browser",
                headers={"Origin": "https://attacker.example"},
            ),
        ):
            pass
        self.assertEqual(raised.exception.code, 4403)

    def test_regular_user_cannot_open_host_terminal_desktop_or_manage_suites(self):
        self.assertEqual(self._setup_admin().status_code, 200)
        auth_service.create_user("alice", "alicepass1", role="user")
        login = self.client.post(
            "/api/auth/login",
            headers={"Origin": "https://testserver"},
            json={"username": "alice", "password": "alicepass1"},
        )
        self.assertEqual(login.status_code, 200)

        terminal = self.client.get("/api/terminal/open")
        desktop = self.client.get("/api/desktop/vnc/status")
        suite_management = self.client.post(
            "/api/test/suites/add-local",
            headers={"Origin": "https://testserver"},
            json={"path": "/tmp"},
        )

        self.assertEqual(terminal.status_code, 403)
        self.assertEqual(desktop.status_code, 403)
        self.assertEqual(suite_management.status_code, 403)

        for path in ("/api/system/websocket/terminal_browser", "/websockify"):
            with (
                self.assertRaises(WebSocketDisconnect) as raised,
                self.client.websocket_connect(
                    path,
                    headers={
                        "Origin": "https://testserver",
                        "Cookie": f"gms_session={self.client.cookies.get('gms_session')}",
                    },
                ),
            ):
                pass
            self.assertEqual(raised.exception.code, 4403)

    def test_worker_token_routes_bypass_browser_session_but_still_validate_token(self):
        previous_service = cluster_api.cluster_service
        repository = ClusterRepository(Path(self.tmp.name) / "cluster.sqlite3")
        cluster_api.cluster_service = ClusterService(repository)
        registration = {
            "worker_id": "worker-246",
            "hostname": "worker-host",
            "address": "192.0.2.10",
            "session_id": "session-1",
        }
        try:
            tokens_path = Path(self.tmp.name) / "worker_tokens_246.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "worker-secret"}}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(tokens_path)},
            ):
                invalid = self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer wrong"},
                    json=registration,
                )
                accepted = self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer worker-secret"},
                    json=registration,
                )
        finally:
            cluster_api.cluster_service = previous_service

        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"], "invalid worker token")
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["worker"]["id"], "worker-246")

    def test_service_authenticated_successes_are_not_audited_but_failures_are(self):
        # Worker heartbeat/poll/register are trusted internal traffic on a
        # hot path (polled every few seconds per worker). Auditing every
        # success grew security_audit.json to 240+ MB. Only failures must
        # land in the audit log.
        import json as _json

        previous_service = cluster_api.cluster_service
        repository = ClusterRepository(Path(self.tmp.name) / "cluster.sqlite3")
        cluster_api.cluster_service = ClusterService(repository)
        registration = {
            "worker_id": "worker-246",
            "hostname": "worker-host",
            "address": "192.0.2.10",
            "session_id": "session-1",
        }
        try:
            tokens_path = Path(self.tmp.name) / "worker_tokens_246.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "worker-secret"}}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(tokens_path)},
            ):
                self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer worker-secret"},
                    json=registration,
                )
                self.client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer wrong"},
                    json=registration,
                )
        finally:
            cluster_api.cluster_service = previous_service

        audit_path = security_audit_logger.log_path
        paths: list[str] = []
        try:
            with open(audit_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        paths.append(_json.loads(line).get("path", ""))
                    except Exception:
                        continue
        except FileNotFoundError:
            pass
        register_successes = [
            p for p in paths
            if p == "/api/cluster/workers/register"
        ]
        # The successful registration must not have been audited; the failed
        # one (status 401) must be.
        self.assertEqual(len(register_successes), 1)

    def test_worker_authenticated_suite_download_is_not_a_public_link(self):
        previous_service = cluster_api.cluster_service
        repository = ClusterRepository(Path(self.tmp.name) / "cluster.sqlite3")
        cluster_api.cluster_service = ClusterService(repository)
        try:
            tokens_path = Path(self.tmp.name) / "worker_tokens_246.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "worker-secret"}}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GMS_WORKER_TOKENS_FILE": str(tokens_path)},
            ):
                invalid = self.client.get(
                    "/api/cluster/suite-library-download/safe/archive.zip",
                    params={"worker_id": "worker-246"},
                    headers={"Authorization": "Bearer wrong"},
                )
                authenticated = self.client.get(
                    "/api/cluster/suite-library-download/safe/archive.zip",
                    params={"worker_id": "worker-246"},
                    headers={"Authorization": "Bearer worker-secret"},
                )
        finally:
            cluster_api.cluster_service = previous_service

        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"], "invalid worker token")
        self.assertEqual(authenticated.status_code, 404)


if __name__ == "__main__":
    unittest.main()
