from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service


class EmailApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.client = TestClient(create_app())

    def tearDown(self):
        self.tmp.cleanup()

    def _login(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1", "display_name": "Admin"},
        )

    def test_send_email_requires_authentication(self):
        response = self.client.post(
            "/api/email/send",
            json={"to": "dev@example.com", "subject": "s", "body": "b"},
        )

        self.assertEqual(response.status_code, 401)

    @patch("features.email.api.send_email")
    def test_authenticated_send_invokes_service(self, send_email_mock):
        self._login()
        send_email_mock.return_value = {
            "sent": True,
            "mode": "smtp",
            "to": ["dev@example.com"],
            "cc": [],
            "recipients": ["dev@example.com"],
        }

        response = self.client.post(
            "/api/email/send",
            json={"to": "dev@example.com", "subject": "s", "body": "b"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        send_email_mock.assert_called_once()

    @patch("features.email.api.send_email")
    def test_recipient_count_is_capped(self, send_email_mock):
        self._login()
        many = [f"user{i}@example.com" for i in range(31)]

        response = self.client.post(
            "/api/email/send",
            json={"to": many, "subject": "s", "body": "b"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("收件人数量超限", response.json()["error"])
        send_email_mock.assert_not_called()

    @patch("features.email.api.send_email")
    def test_recipient_count_cannot_be_bypassed_via_semicolons(self, send_email_mock):
        """单项内嵌分号必须先拆分再计数,否则上限形同虚设。"""
        self._login()
        packed = ";".join(f"user{i}@example.com" for i in range(40))

        response = self.client.post(
            "/api/email/send",
            json={"to": [packed], "subject": "s", "body": "b"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("收件人数量超限", response.json()["error"])
        send_email_mock.assert_not_called()

    @patch("features.email.api.send_email")
    def test_crlf_header_injection_is_rejected(self, send_email_mock):
        """内嵌 CRLF/空白/分隔符的地址在 API 边界被拒绝(头注入防护)。"""
        self._login()

        for bad_to in (
            ["a@b.com\r\nBcc: evil@x.com"],
            ["a b@c.com"],
            ["<script>@x.com"],
            ["a@b.com,extra@y.com;third@z.com,<bad>@x.com"],
        ):
            with self.subTest(to=bad_to):
                response = self.client.post(
                    "/api/email/send", json={"to": bad_to, "subject": "s", "body": "b"}
                )
                self.assertEqual(response.status_code, 400)
                send_email_mock.assert_not_called()

    @patch("features.email.api.send_email")
    def test_subject_crlf_is_stripped_not_rejected(self, send_email_mock):
        """主题中的 CRLF 被折叠为空格(不拒绝合法换行输入,也不注入头)。"""
        self._login()
        send_email_mock.return_value = {"sent": True, "mode": "smtp", "recipients": []}

        response = self.client.post(
            "/api/email/send",
            json={"to": ["dev@example.com"], "subject": "line1\r\nBcc: evil@x.com", "body": "b"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(send_email_mock.call_args[0][1], "line1 Bcc: evil@x.com")

    @patch("features.email.api.send_email")
    def test_body_size_is_capped(self, send_email_mock):
        self._login()

        response = self.client.post(
            "/api/email/send",
            json={
                "to": "dev@example.com",
                "subject": "s",
                "body": "x" * (2_000_001),
            },
        )

        self.assertEqual(response.status_code, 413)
        send_email_mock.assert_not_called()

    @patch("features.email.api.send_email")
    def test_per_user_rate_limit_kicks_in(self, send_email_mock):
        from features.email import api as email_api

        # 重置限流窗口,隔离其他测试的影响。
        email_api._rate_events.clear()
        self._login()
        send_email_mock.return_value = {
            "sent": True,
            "mode": "smtp",
            "to": ["dev@example.com"],
            "cc": [],
            "recipients": ["dev@example.com"],
        }
        statuses = []
        for _ in range(email_api.RATE_LIMIT_MAX_SENDS + 2):
            response = self.client.post(
                "/api/email/send",
                json={"to": "dev@example.com", "subject": "s", "body": "b"},
            )
            statuses.append(response.status_code)
        # 前RATE_LIMIT_MAX_SENDS封成功,之后被 429 拒绝。
        self.assertEqual(
            statuses[: email_api.RATE_LIMIT_MAX_SENDS],
            [200] * email_api.RATE_LIMIT_MAX_SENDS,
        )
        self.assertEqual(statuses[-1], 429)
        self.assertIn("发送过于频繁", response.json()["error"])
        email_api._rate_events.clear()

    def test_send_requires_email_send_permission(self):
        """无 email.send 权限的登录用户被拒(权限细分)。"""
        import features.email.api as email_api
        from features.auth import CurrentUser  # public surface

        self._login()

        def deny(request):
            return CurrentUser(
                id="u-viewer",
                username="viewer",
                display_name="Viewer",
                role="user",
                # user 角色默认有 email.send;extra_permissions 为空且
                # 用一个没有任何权限的角色模拟细分权限被回收的账号。
            )

        # 临时构造 role 权限不含 email.send 的 principal:用 worker_service
        # 角色(只有 worker.*,没有 email.send)。
        def deny_worker(request):
            return CurrentUser(
                id="u-svc",
                username="svc",
                display_name="Service",
                role="worker_service",
            )

        with patch.object(email_api, "require_authenticated_user", deny_worker):
            response = self.client.post(
                "/api/email/send",
                json={"to": "dev@example.com", "subject": "s", "body": "b"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("email.send", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
