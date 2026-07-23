import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import AuthService, auth_service
from foundation.config import config_manager


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized
        self.tmp.cleanup()

    def test_current_user_supports_anonymous_client_identity_in_development(self):
        response = self.client.get("/api/users/current")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["user"])
        self.assertTrue(response.json()["client_id"])

    def test_anonymous_user_cannot_modify_or_list_sensitive_config(self):
        self.assertEqual(
            self.client.post('/api/config/update', json={'local_server': 'x@y'}).status_code,
            401,
        )
        self.assertEqual(
            self.client.get('/api/config/client-ssh-credentials').status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                '/api/config/client-ssh-credentials',
                json={'device_host': 'user@192.0.2.1', 'password': 'secret'},
            ).status_code,
            401,
        )

    def test_setup_creates_admin_session_and_header_cannot_spoof_identity(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1", "display_name": "Admin"},
        )
        self.assertEqual(setup.status_code, 200)
        setup_payload = setup.json()
        self.assertEqual(setup_payload["user"]["role"], "admin")

        response = self.client.get(
            "/api/users/current",
            headers={"X-Client-Username": "attacker"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["client_id"], setup_payload["user"]["id"])
        self.assertNotEqual(payload["client_id"], "attacker")
        self.assertEqual(payload["username"], "admin")

    def test_login_after_setup(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post("/api/auth/logout")

        login = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "strongpass1"},
        )

        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["authenticated"])

    def test_client_login_uses_host_scoped_ssh_credential_as_user_identity(self):
        with patch.object(
            config_manager,
            "find_device_host_password",
            return_value="windows-lock-password",
        ):
            login = self.client.post(
                "/api/auth/login",
                json={
                    "username": "hcq@172.16.14.66",
                    "password": "windows-lock-password",
                },
            )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["username"], "hcq@172.16.14.66")
        self.assertEqual(login.json()["user"]["role"], "user")

    def test_ip_only_client_login_resolves_username_with_ssh_whoami(self):
        with patch.object(
            config_manager,
            "load_config",
            return_value={"client_hosts": {"172.16.14.65": "cp2-share"}},
        ), patch.object(
            config_manager,
            "find_device_host_password",
            return_value=None,
        ), patch(
            "features.auth.api._client_ssh_authenticator",
            return_value=(True, "cp2-share", None),
        ) as detect:
            login = self.client.post(
                "/api/auth/login",
                json={
                    "username": "172.16.14.65",
                    "password": "windows-lock-password",
                },
            )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["username"], "cp2-share@172.16.14.65")
        detect.assert_called_once_with(
            "172.16.14.65",
            "cp2-share",
            "windows-lock-password",
        )

    def test_admin_user_list_reports_real_accounts_and_active_sessions(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(setup.status_code, 200)
        auth_service.create_user("alice", "alicepass1", role="user")

        response = self.client.get("/api/auth/users")

        self.assertEqual(response.status_code, 200)
        users = {item["username"]: item for item in response.json()["users"]}
        self.assertEqual(set(users), {"admin", "alice"})
        self.assertGreaterEqual(users["admin"]["active_session_count"], 1)
        self.assertEqual(users["alice"]["active_session_count"], 0)

    def test_auth_status_recreates_schema_after_data_directory_is_deleted(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(setup.status_code, 200)

        shutil.rmtree(self.tmp.name)

        status = self.client.get("/api/auth/status")

        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["authenticated"])
        self.assertTrue(status.json()["setup_required"])
        with sqlite3.connect(auth_service.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertTrue(auth_service._REQUIRED_TABLES.issubset(tables))

    def test_existing_user_schema_is_migrated_for_device_operator_role(self):
        db_path = Path(self.tmp.name) / "legacy-auth.sqlite3"
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE platform_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    display_name TEXT NOT NULL DEFAULT '',
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO platform_users VALUES (
                    'legacy-admin', 'legacy', 'unused', 'admin', '', 0, ?, ?
                )
                """,
                (now, now),
            )
            conn.commit()

        service = AuthService(db_path)
        service.initialize()
        operator = service.create_user(
            "operator",
            "operator-pass",
            role="device_operator",
        )

        self.assertEqual(operator.role, "device_operator")
        self.assertEqual(service.list_users()[0]["username"], "legacy")

    def test_admin_account_management_requires_elevation_and_revokes_sessions(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(setup.status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/auth/users",
                json={
                    "username": "operator",
                    "password": "operator-pass",
                    "role": "device_operator",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/elevate",
                json={"username": "admin", "password": "strongpass1"},
            ).status_code,
            200,
        )
        created = self.client.post(
            "/api/auth/users",
            json={
                "username": "operator",
                "password": "operator-pass",
                "role": "device_operator",
            },
        )
        self.assertEqual(created.status_code, 200)
        operator_id = created.json()["user"]["id"]
        self.assertIn("devices.lease", created.json()["user"]["permissions"])

        self.client.post("/api/auth/logout")
        login = self.client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "operator-pass"},
        )
        self.assertEqual(login.status_code, 200)
        operator_cookie = self.client.cookies.get("gms_session")

        self.client.cookies.clear()
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "strongpass1"},
        )
        disabled = self.client.patch(
            f"/api/auth/users/{operator_id}",
            json={"disabled": True},
        )
        self.assertEqual(disabled.status_code, 200)

        self.client.cookies.clear()
        self.client.cookies.set("gms_session", operator_cookie)
        status = self.client.get("/api/auth/status")
        self.assertFalse(status.json()["authenticated"])

    def test_last_active_admin_cannot_be_disabled(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        admin_id = setup.json()["user"]["id"]
        self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "strongpass1"},
        )

        response = self.client.patch(
            f"/api/auth/users/{admin_id}",
            json={"disabled": True},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("最后一个管理员", response.json()["error"])

    def _setup_admin_and_user(self):
        """Create an admin (via setup) + a normal user, return session cookies."""
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        # Create a normal user directly in the store, then log it in.
        auth_service.create_user("alice", "alicepass1", role="user")
        admin_cookie = self.client.cookies.get("gms_session")
        # Log out admin, log in alice to grab her cookie.
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "alice", "password": "alicepass1"})
        user_cookie = self.client.cookies.get("gms_session")
        return admin_cookie, user_cookie

    def _set_session(self, cookie_value):
        if cookie_value:
            self.client.cookies.set("gms_session", cookie_value)
        else:
            self.client.cookies.clear()

    def test_admin_must_reauthenticate_before_sensitive_operation(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertFalse(self.client.get("/api/auth/status").json()["elevated"])

        resp = self.client.request("DELETE", "/api/users/remove", json={"ip": "1.2.3.4"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.json()["detail"]["elevation_required"])

    def test_admin_elevation_lasts_for_session_and_clears_on_new_session(self):
        # 二次认证仅在当前会话有效。
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        elevated = self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "strongpass1"},
        )

        self.assertEqual(elevated.status_code, 200)
        status = self.client.get("/api/auth/status").json()
        self.assertTrue(status["elevated"])
        elevated_until = datetime.fromisoformat(status["elevated_until"])
        self.assertGreater(elevated_until, datetime.now(timezone.utc) + timedelta(hours=1))

        # A new session (logout + login) starts non-elevated.
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "strongpass1"})
        self.assertFalse(self.client.get("/api/auth/status").json()["elevated"])

    def test_browser_tab_reset_clears_elevation_without_logging_out_client(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/elevate",
                json={"username": "admin", "password": "strongpass1"},
            ).status_code,
            200,
        )
        reset = self.client.post("/api/auth/elevation/reset")
        self.assertEqual(reset.status_code, 200)
        status = self.client.get("/api/auth/status").json()
        self.assertTrue(status["authenticated"])
        self.assertFalse(status["elevated"])

    def test_normal_user_not_elevated_and_blocked_from_sensitive_endpoint(self):
        # The elevation-on-login only applies to admins; a normal user stays
        # non-elevated and the sensitive endpoint still demands elevation.
        _admin_cookie, user_cookie = self._setup_admin_and_user()
        self._set_session(user_cookie)

        self.assertFalse(self.client.get("/api/auth/status").json()["elevated"])
        resp = self.client.request("DELETE", "/api/users/remove", json={"ip": "1.2.3.4"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.json()["detail"]["elevation_required"])

    def test_elevation_rejects_non_admin(self):
        _admin_cookie, user_cookie = self._setup_admin_and_user()
        self._set_session(user_cookie)
        # A normal user cannot elevate even with their own correct password.
        elev = self.client.post(
            "/api/auth/elevate",
            json={"username": "alice", "password": "alicepass1"},
        )
        self.assertEqual(elev.status_code, 403)
        self.assertFalse(elev.json().get("elevated", False))

    def test_client_session_can_be_elevated_by_admin_without_changing_client_role(self):
        _admin_cookie, user_cookie = self._setup_admin_and_user()
        self._set_session(user_cookie)

        elev = self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "strongpass1"},
        )

        self.assertEqual(elev.status_code, 200)
        self.assertTrue(elev.json()["elevated"])
        self.assertTrue(elev.json()["admin_verified"])
        self.assertEqual(elev.json()["user"]["username"], "alice")
        self.assertEqual(elev.json()["user"]["role"], "user")
        self.assertEqual(self.client.get("/api/auth/status").json()["user"]["role"], "user")

    def test_anonymous_development_step_up_creates_admin_session(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post("/api/auth/logout")

        elev = self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "strongpass1"},
        )

        self.assertEqual(elev.status_code, 200)
        self.assertTrue(elev.json()["elevated"])
        self.assertEqual(elev.json()["user"]["role"], "admin")
        status = self.client.get("/api/auth/status").json()
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["elevated"])

    def test_login_failures_are_rate_limited_persistently(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post("/api/auth/logout")

        with patch("features.auth.rate_limit.AUTH_MAX_ACCOUNT_IP_FAILURES", 2):
            first = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-password"},
            )
            blocked = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-password"},
            )
            still_blocked = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "strongpass1"},
            )

        self.assertEqual(first.status_code, 401)
        self.assertEqual(blocked.status_code, 429)
        self.assertTrue(int(blocked.headers["Retry-After"]) > 0)
        self.assertEqual(still_blocked.status_code, 429)


if __name__ == "__main__":
    unittest.main()
