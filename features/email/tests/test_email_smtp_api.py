"""SMTP 管理 API 测试：脱敏回显、admin 门控、密码加密落盘。"""

from __future__ import annotations

import unittest
from unittest import mock

from cryptography.fernet import Fernet

from features.auth import CurrentUser
from features.email.api import SmtpConfigRequest, get_smtp_config, save_smtp_config


ADMIN = CurrentUser(id="u1", username="admin", role="admin")


class SmtpApiTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(
            "os.environ",
            {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")},
        )
        env.start()
        self.addCleanup(env.stop)

    def _masked(self, response):
        """JSONResponse -> dict（测试断言用）。"""
        import json

        return json.loads(response.body.decode("utf-8"))

    def test_save_then_get_masks_password(self):
        saved: dict = {}

        def fake_update(updates, *, remove_keys=None):
            saved.update(updates)
            return True

        with mock.patch(
            "foundation.config.config_manager.load_config", return_value={}
        ), mock.patch(
            "foundation.config.config_manager.update_runtime_config",
            side_effect=fake_update,
        ):
            result = self._masked(save_smtp_config(
                SmtpConfigRequest(
                    smtp_host="smtp.x.com", smtp_port=465,
                    username="ops@x.com", password="plain-secret",
                ),
                admin=ADMIN,
            ))
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["password_set"])
        email = saved["redmine_dashboard"]["email"]
        self.assertNotIn("password", email)  # 明文永不落盘
        self.assertIn("encrypted_password", email)

    def test_use_ssl_follows_submitted_value_not_port(self):
        # use_ssl 必须按提交值保存，不能只按端口 465 推断。
        saved: dict = {}

        def fake_update(updates, *, remove_keys=None):
            saved.update(updates)
            return True

        with mock.patch(
            "foundation.config.config_manager.load_config", return_value={}
        ), mock.patch(
            "foundation.config.config_manager.update_runtime_config",
            side_effect=fake_update,
        ):
            # 非 465 端口 + 显式 SSL。
            self._masked(save_smtp_config(
                SmtpConfigRequest(
                    smtp_host="smtp.x.com", smtp_port=938,
                    username="ops@x.com", use_ssl=True,
                ),
                admin=ADMIN,
            ))
        email = saved["redmine_dashboard"]["email"]
        self.assertTrue(email["use_ssl"])
        self.assertNotIn("use_tls", email)

        with mock.patch(
            "foundation.config.config_manager.load_config", return_value={}
        ), mock.patch(
            "foundation.config.config_manager.update_runtime_config",
            side_effect=fake_update,
        ):
            # 465 端口 + 显式关 SSL（自签/代理场景）→ 记 use_tls。
            self._masked(save_smtp_config(
                SmtpConfigRequest(
                    smtp_host="smtp.x.com", smtp_port=465,
                    username="ops@x.com", use_ssl=False,
                ),
                admin=ADMIN,
            ))
        email = saved["redmine_dashboard"]["email"]
        self.assertFalse(email["use_ssl"])
        self.assertTrue(email["use_tls"])

    def test_mask_keeps_existing_password(self):
        existing = {"redmine_dashboard": {"email": {
            "smtp_host": "old.x.com",
            "encrypted_password": "gAAAA",
        }}}

        with mock.patch(
            "foundation.config.config_manager.load_config",
            side_effect=[existing, existing],
        ), mock.patch(
            "foundation.config.config_manager.update_runtime_config",
            return_value=True,
        ):
            result = self._masked(save_smtp_config(
                SmtpConfigRequest(smtp_host="new.x.com", password="***"),
                admin=ADMIN,
            ))
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["password_set"])

    def test_get_requires_authentication(self):
        with mock.patch(
            "features.email.api.require_authenticated_user",
            side_effect=PermissionError("unauthenticated"),
        ), self.assertRaises(PermissionError):
            get_smtp_config(mock.MagicMock())

    def test_invalid_username_rejected(self):
        with mock.patch(
            "foundation.config.config_manager.load_config", return_value={}
        ):
            result = self._masked(save_smtp_config(
                SmtpConfigRequest(smtp_host="h", username="not-an-email"),
                admin=ADMIN,
            ))
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
