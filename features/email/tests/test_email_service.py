"""features.email.service 的单元测试。

用 fake manager 注入 SMTP 配置，mock smtplib 验证各分支，避免依赖全局 config 文件。
"""

from __future__ import annotations

import os
import smtplib
import tempfile
import unittest
from email import message_from_string
from unittest.mock import MagicMock, patch

from features.email.service import send_email, split_emails


def _fake_manager(email_cfg: dict):
    """构造一个内存 config manager，load_config 返回给定 email 段。"""
    mgr = MagicMock()
    mgr.load_config.return_value = {"redmine_dashboard": {"email": email_cfg}}
    return mgr


# 隔离全局配置：resolve_email_config 的全局默认/兼容兜底读取真实 runtime，
# 部署机上真实 SMTP 的非空字段会覆盖测试 fixture，必须屏蔽。
_NO_GLOBAL = patch(
    "foundation.config.config_manager.load_config", return_value={}
)
_NO_GLOBAL.start()
_NO_GLOBAL_stop = _NO_GLOBAL.stop

# 隔离发件台账：send_email 管线每次调用都写 outbox 台账，data_root 默认
# 指向仓库 data/——不隔离的话单测 fixture（"s"/"b"、头注入样例等）会
# 污染生产台账（曾以「系统」发件人出现在历史邮件页面）。模块加载即把
# store 的 data_root 指到临时目录并重置单例。
import tempfile as _tempfile
from pathlib import Path as _Path
from types import SimpleNamespace as _SimpleNamespace

from features.email import store as _store_module
from features.email.store import reset_email_outbox_store as _reset_outbox

_store_data_root_patch = patch.object(
    _store_module,
    "settings",
    _SimpleNamespace(data_root=_Path(_tempfile.mkdtemp(prefix="gms-email-test-"))),
)
_store_data_root_patch.start()
_reset_outbox()


QIYE_CFG = {
    "smtp_host": "smtphz.qiye.163.com",
    "smtp_port": 465,
    "from_addr": "me@rock-chips.com",
    "username": "me@rock-chips.com",
    "password": "smtp-auth-code",
}


class EmailPasswordResolutionTests(unittest.TestCase):
    """SMTP 密码读取：密文优先解密，遗留明文仅作兼容兜底。"""

    def setUp(self):
        from cryptography.fernet import Fernet

        self._env = patch.dict(
            "os.environ",
            {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_encrypted_password_is_decrypted(self):
        from features.email.service import _email_password
        from foundation.secrets import encrypt_secret

        cfg = {"encrypted_password": encrypt_secret("smtp-auth-code")}
        self.assertEqual(_email_password(cfg), "smtp-auth-code")

    def test_legacy_plaintext_is_still_read(self):
        from features.email.service import _email_password

        self.assertEqual(_email_password({"password": "legacy"}), "legacy")

    def test_unreadable_ciphertext_fails_closed(self):
        from features.email.service import _email_password

        self.assertEqual(_email_password({"encrypted_password": "not-a-token"}), "")


class SplitEmailsTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(split_emails(""), [])
        self.assertEqual(split_emails(None), [])
        self.assertEqual(split_emails([]), [])

    def test_comma_and_semicolon(self):
        self.assertEqual(
            split_emails("a@x.com, b@x.com; c@x.com"),
            ["a@x.com", "b@x.com", "c@x.com"],
        )

    def test_list_with_mixed_separators(self):
        self.assertEqual(
            split_emails(["a@x.com, b@x.com", "c@x.com;d@x.com"]),
            ["a@x.com", "b@x.com", "c@x.com", "d@x.com"],
        )

    def test_strips_whitespace(self):
        self.assertEqual(split_emails("  a@x.com  ,  "), ["a@x.com"])


class SendEmailTests(unittest.TestCase):
    def test_unconfigured_when_no_smtp_host(self):
        result = send_email("a@x.com", "s", "b", manager=_fake_manager({}))
        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "unconfigured")
        self.assertIn("SMTP", result["error"])

    def test_global_kill_switch_disabled_mode(self):
        result = send_email(
            "a@x.com", "s", "b", manager=_fake_manager({**QIYE_CFG, "enabled": False})
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "disabled")
        self.assertIn("全局关闭", result["error"])

    def test_enabled_flag_defaults_to_on(self):
        with patch.dict("os.environ", {"GMS_EMAIL_ENABLED": "0"}):
            result = send_email(
                "a@x.com", "s", "b", manager=_fake_manager(QIYE_CFG)
            )
        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "disabled")

        with patch.dict("os.environ", {"GMS_EMAIL_ENABLED": "1"}):
            result = send_email(
                "", "s", "b", manager=_fake_manager({**QIYE_CFG, "enabled": False})
            )
        # 环境变量显式开启可覆盖配置关闭；空收件人报错说明已越过开关。
        self.assertIn("收件人", result.get("error", ""))

    def test_empty_recipients_rejected(self):
        result = send_email("", "s", "b", manager=_fake_manager(QIYE_CFG))
        self.assertFalse(result["sent"])
        self.assertIn("收件人", result["error"])

    @patch("features.email.transport.smtplib.SMTP_SSL")
    def test_header_injection_is_rejected_or_collapsed_for_all_callers(self, smtp_cls):
        invalid = send_email(
            "dev@example.com\r\nBcc: evil@example.com",
            "subject",
            "body",
            manager=_fake_manager(QIYE_CFG),
        )
        self.assertFalse(invalid["sent"])
        smtp_cls.assert_not_called()

        smtp = smtp_cls.return_value.__enter__.return_value
        result = send_email(
            "dev@example.com",
            "line 1\r\nBcc: evil@example.com",
            "body",
            sender_name="Daily\r\nReply-To: evil@example.com",
            manager=_fake_manager(QIYE_CFG),
        )
        self.assertTrue(result["sent"])
        raw = smtp.sendmail.call_args.args[2]
        message = message_from_string(raw)
        self.assertEqual(message["Subject"], "line 1 Bcc: evil@example.com")
        self.assertIsNone(message["Bcc"])
        self.assertIsNone(message["Reply-To"])

    def test_qiye_163_requires_username_and_password(self):
        cfg = {"smtp_host": "smtphz.qiye.163.com", "smtp_port": 465}
        result = send_email("a@x.com", "s", "b", manager=_fake_manager(cfg))
        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "unconfigured")
        self.assertIn("授权码", result["error"])

    def test_qiye_163_forces_from_equals_username(self):
        cfg = dict(QIYE_CFG)
        cfg["from_addr"] = "other@rock-chips.com"  # 与 username 不符
        with patch("features.email.transport.smtplib.SMTP_SSL") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            result = send_email("a@x.com", "主题", "正文", manager=_fake_manager(cfg))

            self.assertTrue(result["sent"])
            # sendmail 的第一个参数是 from_addr，应被强制对齐为 username
            from_addr, _recipients, _ = smtp.sendmail.call_args.args
            self.assertEqual(from_addr, "me@rock-chips.com")

    @patch("features.email.transport.smtplib.SMTP_SSL")
    def test_plain_text_send_with_cc_dedup(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        result = send_email(
            "a@x.com, b@x.com",
            "主题",
            "正文",
            cc="b@x.com;c@x.com",  # b@x.com 与 to 重复，应去重
            manager=_fake_manager(QIYE_CFG),
        )

        self.assertTrue(result["sent"])
        self.assertEqual(result["mode"], "smtp")
        self.assertEqual(result["to"], ["a@x.com", "b@x.com"])
        self.assertEqual(result["cc"], ["b@x.com", "c@x.com"])
        _from, recipients, _raw = smtp.sendmail.call_args.args
        self.assertEqual(set(recipients), {"a@x.com", "b@x.com", "c@x.com"})
        smtp.login.assert_called_once_with("me@rock-chips.com", "smtp-auth-code")

    @patch("features.email.transport.smtplib.SMTP_SSL")
    def test_html_body_uses_multipart(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        send_email("a@x.com", "主题", "<p>hi</p>", is_html=True, manager=_fake_manager(QIYE_CFG))

        _from, _recipients, raw = smtp.sendmail.call_args.args
        msg = message_from_string(raw)
        self.assertTrue(msg.is_multipart())
        subtypes = [part.get_content_type() for part in msg.walk() if part is not msg]
        self.assertIn("text/html", subtypes)

    @patch("features.email.transport.smtplib.SMTP_SSL")
    def test_sender_name_display(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        send_email("a@x.com", "主题", "正文", sender_name="AI周报", manager=_fake_manager(QIYE_CFG))

        _from, _recipients, raw = smtp.sendmail.call_args.args
        msg = message_from_string(raw)
        # 中文昵称会被 MIME 编码为 RFC 2047（=?utf-8?b?...?=），解码后比较
        from email.header import decode_header, make_header
        decoded = str(make_header(decode_header(msg["From"])))
        self.assertEqual(decoded, "AI周报 <me@rock-chips.com>")

    @patch("features.email.transport.smtplib.SMTP_SSL")
    def test_attachment_present_and_missing(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        with tempfile.TemporaryDirectory() as tmp:
            good_path = os.path.join(tmp, "weekly.md")
            with open(good_path, "wb") as fh:
                fh.write(b"# weekly report")
            result = send_email(
                "a@x.com", "主题", "正文",
                attachment_paths=[good_path, "/nonexistent/file.xyz"],
                allowed_attachment_roots=[tmp],
                manager=_fake_manager(QIYE_CFG),
            )
            self.assertTrue(result["sent"])
            _from, _recipients, raw = smtp.sendmail.call_args.args
            msg = message_from_string(raw)
            attachments = [
                part.get_filename()
                for part in msg.walk()
                if part.get_content_disposition() == "attachment"
            ]
            self.assertEqual(len(attachments), 1)
            self.assertIn("/nonexistent/file.xyz", result["attachments_missing"][0])

    @patch("features.email.transport.smtplib.SMTP_SSL")
    def test_attachment_paths_blocked_without_allowed_roots(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        with tempfile.NamedTemporaryFile(delete=False, suffix=".md") as fh:
            fh.write(b"# private")
            blocked_path = fh.name
        try:
            result = send_email(
                "a@x.com", "主题", "正文",
                attachment_paths=[blocked_path],
                manager=_fake_manager(QIYE_CFG),
            )

            self.assertTrue(result["sent"])
            self.assertIn(blocked_path, result["attachments_blocked"])
            _from, _recipients, raw = smtp.sendmail.call_args.args
            msg = message_from_string(raw)
            attachments = [
                part.get_filename()
                for part in msg.walk()
                if part.get_content_disposition() == "attachment"
            ]
            self.assertEqual(attachments, [])
        finally:
            os.unlink(blocked_path)

    @patch("features.email.transport.smtplib.SMTP_SSL")
    def test_smtp_auth_error_returned(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"auth failed")
        result = send_email("a@x.com", "主题", "正文", manager=_fake_manager(QIYE_CFG))

        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "smtp")
        self.assertIn("SMTP认证失败", result["error"])

    @patch("features.email.transport.smtplib.SMTP")
    def test_non_ssl_uses_starttls(self, smtp_cls):
        """非 SSL 端口应走 SMTP + STARTTLS（use_ssl 默认由端口判定）。"""
        cfg = {
            "smtp_host": "smtp.company.com",  # 非 qiye.163，不强制授权码校验
            "smtp_port": 587,
            "username": "me@company.com",
            "password": "pwd",
            "use_tls": True,
        }
        smtp = smtp_cls.return_value.__enter__.return_value
        result = send_email("a@x.com", "主题", "正文", manager=_fake_manager(cfg))

        self.assertTrue(result["sent"])
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once()


if __name__ == "__main__":
    unittest.main()
