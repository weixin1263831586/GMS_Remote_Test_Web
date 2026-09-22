"""Regression coverage for the two-account browser switcher."""

import unittest
from unittest.mock import patch

from features.auth.tests._auth_fixture import AuthApiMixin
from foundation.config import config_manager


class AuthSwitchTargetTests(AuthApiMixin, unittest.TestCase):
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

    def test_client_form_username_with_local_password_still_requires_ssh_verifier(self):
        # 凭据语义硬边界：即使本地表里存在可用 PBKDF2 校验的 client 形态
        # 账号，本地密码也绝不能通过登录——必须走 SSH credential verifier。
        from features.auth import auth_service

        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        auth_service.create_client_user("hcq@172.16.14.66")
        # 模拟"本地表被写入可校验密码"（管理员重置/历史遗留行）。不经
        # set_user_password——那条路现在本身就会拒绝 client 形态账号。
        with auth_service._connect() as conn:
            conn.execute(
                """
                UPDATE platform_users
                SET password_hash = ?
                WHERE username = ?
                """,
                (auth_service._hash_password("leaked-local-pass"), "hcq@172.16.14.66"),
            )
            conn.commit()
        self.client.post("/api/auth/logout")

        # 两个请求都需绑定来源 IP 172.16.14.66：身份检查在凭据校验之前，
        # 未 patch 时 testclient 的 127.0.0.1 会先命中 403 账号不匹配。
        # 存储凭据比对失败（而非真实 SSH 探测）驱动 401。
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
            rejected = self.client.post(
                "/api/auth/login",
                json={
                    "username": "hcq@172.16.14.66",
                    "password": "leaked-local-pass",
                    "account_switch_target": "client",
                },
            )
            self.assertEqual(rejected.status_code, 401)

            # 正确的 SSH 凭据仍可通过（存储凭据比对分支）。
            allowed = self.client.post(
                "/api/auth/login",
                json={
                    "username": "hcq@172.16.14.66",
                    "password": "windows-lock-password",
                    "account_switch_target": "client",
                },
            )
            self.assertEqual(allowed.status_code, 200)

    def test_create_user_rejects_client_form_local_password(self):
        from features.auth import auth_service

        with self.assertRaises(ValueError):
            auth_service.create_user("hcq@172.16.14.66", "some-local-pass")

    def test_set_user_password_rejects_client_form_account(self):
        from features.auth import auth_service

        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        client_user = auth_service.create_client_user("hcq@172.16.14.66")

        with self.assertRaises(ValueError):
            auth_service.set_user_password(client_user.id, "some-local-pass")

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
