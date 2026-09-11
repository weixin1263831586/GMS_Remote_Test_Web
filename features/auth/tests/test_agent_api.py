"""Agent Bearer auth + approval-token API boundary tests (2026-09-08 audit).

Exercises the FastAPI endpoints end to end with the TestClient:
- POST /api/auth/agent-tokens (elevated admin) returns a one-time raw token
- Bearer <agent-token> authenticates with scopes, without any cookie
- /api/auth/approval-tokens requires a human session (agent tokens denied)
- /api/auth/approval-tokens/consume validates tool/device/command binding
- /api/auth/agent-enroll exchanges a one-shot enrollment code without a session
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service


class AgentBearerAuthTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self._tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        auth_service.initialize()
        self.client = TestClient(create_app())
        if auth_service.setup_required():
            resp = self.client.post(
                "/api/auth/setup",
                json={"username": "admin", "password": "password123"},
            )
            assert resp.status_code == 200, resp.text
        self._admin_login()

    def tearDown(self):
        self.client.close()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized

    def _admin_login(self):
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 200, resp.text
        # Token minting / enrollment require the elevated-admin gate.
        resp = self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 200, resp.text

    def _create_token(self, scopes):
        resp = self.client.post(
            "/api/auth/agent-tokens",
            json={
                "name": "codex-build01",
                "scopes": scopes,
                "allowed_workers": "*",
                "expires_days": 30,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def test_create_token_requires_elevation_and_returns_raw_once(self):
        # Elevated admin session from setup works.
        record = self._create_token(["devices.read", "tests.execute"])
        self.assertIn("token", record)
        # Listing must not include the raw token.
        resp = self.client.get("/api/auth/agent-tokens")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(
            record["token"], resp.text, "raw token leaked via list endpoint"
        )

    def test_bearer_token_authenticates_without_cookie(self):
        record = self._create_token(["devices.read"])
        headers = {"Authorization": f"Bearer {record['token']}"}
        resp = self.client.get("/api/auth/status", headers=headers)
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["role"], "agent_service")
        self.assertIn("devices.read", payload["user"]["permissions"])

    def test_invalid_bearer_token_fails_closed(self):
        headers = {"Authorization": "Bearer not-a-real-token"}
        resp = self.client.get(
            "/api/devices/list", headers=headers
        )
        # auth-required deployments: invalid bearer must not fall back to
        # anonymous/dev mode. Accept 401/403 but never 200.
        self.assertIn(resp.status_code, (401, 403))

    def test_agent_token_cannot_mint_approvals(self):
        record = self._create_token(["devices.read", "tests.execute", "devices.use_leased"])
        headers = {"Authorization": f"Bearer {record['token']}"}
        resp = self.client.post(
            "/api/auth/approval-tokens",
            headers=headers,
            json={"tool": "gms_rt_shell_exec", "device": "D1", "command": "reboot"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_human_session_can_create_and_consume_approval(self):
        resp = self.client.post(
            "/api/auth/approval-tokens",
            json={
                "tool": "gms_rt_shell_exec",
                "device": "RK3572",
                "command": "settings put global wifi_on 1",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["approval"]["token"]
        # Correct consume (with cookie session).
        resp = self.client.post(
            "/api/auth/approval-tokens/consume",
            json={
                "token": token,
                "tool": "gms_rt_shell_exec",
                "device": "RK3572",
                "command": "settings put global wifi_on 1",
            },
        )
        self.assertEqual(resp.status_code, 200)
        # Single use.
        resp = self.client.post(
            "/api/auth/approval-tokens/consume",
            json={
                "token": token,
                "tool": "gms_rt_shell_exec",
                "device": "RK3572",
                "command": "settings put global wifi_on 1",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_consume_rejects_command_swap(self):
        resp = self.client.post(
            "/api/auth/approval-tokens",
            json={
                "tool": "gms_rt_shell_exec",
                "device": "RK3572",
                # User approves a harmless command...
                "command": "settings put global wifi_on 1",
            },
        )
        token = resp.json()["approval"]["token"]
        # ...agent tries to run something destructive instead.
        resp = self.client.post(
            "/api/auth/approval-tokens/consume",
            json={
                "token": token,
                "tool": "gms_rt_shell_exec",
                "device": "RK3572",
                "command": "rm -rf /data",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_enrollment_flow_without_password(self):
        resp = self.client.post(
            "/api/auth/agent-enrollment-codes",
            json={
                "name": "codex-build03",
                "scopes": ["devices.read", "tests.execute"],
                "allowed_workers": "w1",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        code = resp.json()["enrollment"]["code"]
        # Enroll without any session cookie (fresh client).
        bare = TestClient(create_app())
        self.addCleanup(bare.close)
        resp = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["token"]["token"]
        self.assertIn("tests.execute", resp.json()["token"]["scopes"])
        # The code is one-shot.
        resp = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(resp.status_code, 403)
        # And the enrolled token authenticates.
        resp = bare.get(
            "/api/auth/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertTrue(resp.json()["authenticated"])


if __name__ == "__main__":
    unittest.main()
