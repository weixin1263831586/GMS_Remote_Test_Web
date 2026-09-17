"""Regression coverage for the two-account browser switcher."""

from unittest.mock import patch

from features.auth.tests.test_auth_api import AuthApiTests
from foundation.config import config_manager


class AuthSwitchTargetTests(AuthApiTests):
    def test_auth_status_exposes_the_sole_enabled_admin_for_account_switching(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )

        status = self.client.get("/api/auth/status").json()

        self.assertEqual(status["default_admin_username"], "admin")

    def test_admin_switch_target_rejects_an_ordinary_account(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        from features.auth import auth_service

        auth_service.create_user("ordinary", "ordinary-pass", role="user")
        self.client.post("/api/auth/logout")

        rejected = self.client.post(
            "/api/auth/login",
            json={
                "username": "ordinary", "password": "ordinary-pass",
                "account_switch_target": "admin",
            },
        )
        allowed = self.client.post(
            "/api/auth/login",
            json={
                "username": "admin", "password": "strongpass1",
                "account_switch_target": "admin",
            },
        )

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["user"]["role"], "admin")

    def test_client_switch_target_only_accepts_the_current_client_identity(self):
        with patch.object(
            config_manager,
            "get_runtime_config",
            return_value={"client_hosts": {"172.16.14.66": "hcq"}},
        ), patch.object(
            config_manager,
            "find_device_host_password",
            return_value="windows-lock-password",
        ), patch(
            "features.auth.api._request_source_ip",
            return_value="172.16.14.66",
        ):
            rejected = self.client.post(
                "/api/auth/login",
                json={
                    "username": "other@172.16.14.66",
                    "password": "windows-lock-password",
                    "account_switch_target": "client",
                },
            )
            allowed = self.client.post(
                "/api/auth/login",
                json={
                    "username": "hcq@172.16.14.66",
                    "password": "windows-lock-password",
                    "account_switch_target": "client",
                },
            )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["user"]["username"], "hcq@172.16.14.66")

    def test_client_switch_target_accepts_source_bound_ssh_login_without_mapping(self):
        with patch.object(
            config_manager,
            "get_runtime_config",
            return_value={},
        ), patch.object(
            config_manager,
            "find_device_host_password",
            return_value="windows-lock-password",
        ), patch(
            "features.auth.api._request_source_ip",
            return_value="172.16.14.66",
        ):
            login = self.client.post(
                "/api/auth/login",
                json={
                    "username": "hcq@172.16.14.66",
                    "password": "windows-lock-password",
                    "account_switch_target": "client",
                },
            )

        self.assertEqual(login.status_code, 200)
