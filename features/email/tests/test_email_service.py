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


QIYE_CFG = {
    "smtp_host": "smtphz.qiye.163.com",
    "smtp_port": 465,
    "from_addr": "me@rock-chips.com",
    "username": "me@rock-chips.com",
    "password": "smtp-auth-code",
}


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
        self.assertIn("smtp_host", result["error"])

    def test_empty_recipients_rejected(self):
        result = send_email("", "s", "b", manager=_fake_manager(QIYE_CFG))
        self.assertFalse(result["sent"])
        self.assertIn("收件人", result["error"])

    def test_qiye_163_requires_username_and_password(self):
        cfg = {"smtp_host": "smtphz.qiye.163.com", "smtp_port": 465}
        result = send_email("a@x.com", "s", "b", manager=_fake_manager(cfg))
        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "unconfigured")
        self.assertIn("授权码", result["error"])

    def test_qiye_163_forces_from_equals_username(self):
        cfg = dict(QIYE_CFG)
        cfg["from_addr"] = "other@rock-chips.com"  # 与 username 不符
        with patch("features.email.service.smtplib.SMTP_SSL") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            result = send_email("a@x.com", "主题", "正文", manager=_fake_manager(cfg))

            self.assertTrue(result["sent"])
            # sendmail 的第一个参数是 from_addr，应被强制对齐为 username
            from_addr, recipients, _ = smtp.sendmail.call_args.args
            self.assertEqual(from_addr, "me@rock-chips.com")

    @patch("features.email.service.smtplib.SMTP_SSL")
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
        # recipients 去重后 a/b/c 三个
        _from, recipients, _raw = smtp.sendmail.call_args.args
        self.assertEqual(set(recipients), {"a@x.com", "b@x.com", "c@x.com"})
        # 登录用授权码
        smtp.login.assert_called_once_with("me@rock-chips.com", "smtp-auth-code")

    @patch("features.email.service.smtplib.SMTP_SSL")
    def test_html_body_uses_multipart(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        send_email("a@x.com", "主题", "<p>hi</p>", is_html=True, manager=_fake_manager(QIYE_CFG))

        _from, _recipients, raw = smtp.sendmail.call_args.args
        msg = message_from_string(raw)
        self.assertTrue(msg.is_multipart())
        # 子部分应为 text/html
        subtypes = [part.get_content_type() for part in msg.walk() if part is not msg]
        self.assertIn("text/html", subtypes)

    @patch("features.email.service.smtplib.SMTP_SSL")
    def test_sender_name_display(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        send_email("a@x.com", "主题", "正文", sender_name="AI周报", manager=_fake_manager(QIYE_CFG))

        _from, _recipients, raw = smtp.sendmail.call_args.args
        msg = message_from_string(raw)
        # 中文昵称会被 MIME 编码为 RFC 2047（=?utf-8?b?...?=），解码后比较
        from email.header import decode_header, make_header
        decoded = str(make_header(decode_header(msg["From"])))
        self.assertEqual(decoded, "AI周报 <me@rock-chips.com>")

    @patch("features.email.service.smtplib.SMTP_SSL")
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
            # 存在的附件被附上，缺失的记录在 attachments_missing
            _from, _recipients, raw = smtp.sendmail.call_args.args
            msg = message_from_string(raw)
            attachments = [
                part.get_filename()
                for part in msg.walk()
                if part.get_content_disposition() == "attachment"
            ]
            self.assertEqual(len(attachments), 1)
            self.assertIn("/nonexistent/file.xyz", result["attachments_missing"][0])

    @patch("features.email.service.smtplib.SMTP_SSL")
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

    @patch("features.email.service.smtplib.SMTP_SSL")
    def test_smtp_auth_error_returned(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"auth failed")
        result = send_email("a@x.com", "主题", "正文", manager=_fake_manager(QIYE_CFG))

        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "smtp")
        self.assertIn("SMTP认证失败", result["error"])

    @patch("features.email.service.smtplib.SMTP")
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
