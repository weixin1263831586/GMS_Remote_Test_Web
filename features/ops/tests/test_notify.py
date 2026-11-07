"""Notification transport tests (phase 2): formats, signature, dispatch."""

import hashlib
import hmac
import unittest
from unittest import mock

from features.ops.notify import (
    _webhook_payload,
    load_ops_alerts_config,
    notify_incident,
    send_webhook,
)


class FormatTests(unittest.TestCase):
    def test_generic_payload_wraps_incident(self):
        payload = _webhook_payload({"kind": "disk_pressure"}, "generic")
        self.assertEqual(payload["source"], "gms-ats-ops")
        self.assertEqual(payload["incident"]["kind"], "disk_pressure")

    def test_wecom_dingtalk_feishu_envelopes(self):
        self.assertEqual(_webhook_payload({}, "wecom")["msgtype"], "markdown")
        dingtalk = _webhook_payload({}, "dingtalk")
        self.assertEqual(dingtalk["msgtype"], "markdown")
        self.assertIn("text", dingtalk["markdown"])
        self.assertEqual(_webhook_payload({}, "feishu")["msg_type"], "text")


class WebhookTests(unittest.TestCase):
    def test_signature_covers_body(self):
        captured = {}

        class Response:
            status_code = 200

        def fake_post(url, content=None, headers=None, timeout=None, **_kwargs):
            captured.update({"url": url, "content": content, "headers": headers})
            return Response()

        with mock.patch("httpx.post", side_effect=fake_post):
            result = send_webhook(
                {"kind": "k", "message": "m"},
                {"webhook_url": "https://example/hook", "webhook_format": "generic",
                 "webhook_token_file": ""},
            )
        self.assertTrue(result["sent"])
        self.assertNotIn("X-GMS-Ops-Signature", captured["headers"])

    def test_signature_present_with_token_file(self):
        import tempfile
        from pathlib import Path

        captured = {}

        class Response:
            status_code = 200

        def fake_post(url, content=None, headers=None, timeout=None, **_kwargs):
            captured.update({"content": content, "headers": headers})
            return Response()

        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            token_path.write_bytes(b"sekret")
            with mock.patch("httpx.post", side_effect=fake_post):
                result = send_webhook(
                    {"kind": "k"},
                    {"webhook_url": "https://example/hook", "webhook_format": "generic",
                     "webhook_token_file": str(token_path)},
                )
        self.assertTrue(result["sent"])
        expected = hmac.new(
            b"sekret", captured["content"], hashlib.sha256
        ).hexdigest()
        self.assertEqual(
            captured["headers"]["X-GMS-Ops-Signature"], f"sha256={expected}"
        )

    def test_missing_url_is_skipped(self):
        self.assertEqual(
            send_webhook({}, {"webhook_url": ""})["reason"], "no webhook_url"
        )

    def test_http_error_is_reported_not_raised(self):
        class Response:
            status_code = 500

        with mock.patch("httpx.post", return_value=Response()):
            result = send_webhook({}, {"webhook_url": "https://example/hook"})
        self.assertFalse(result["sent"])
        self.assertEqual(result["status_code"], 500)


class DispatchTests(unittest.TestCase):
    def test_disabled_config_sends_nothing(self):
        self.assertEqual(
            notify_incident({"kind": "k"}, cfg={"enabled": False})["reason"],
            "ops alerts disabled",
        )

    def test_enabled_config_calls_webhook(self):
        with mock.patch(
            "features.ops.notify.send_webhook", return_value={"sent": True}
        ) as hook:
            result = notify_incident(
                {"kind": "k"},
                cfg={"enabled": True, "webhook_url": "https://x", "email_to": ""},
            )
        self.assertTrue(result["ok"])
        hook.assert_called_once()

    def test_env_overrides(self):
        with mock.patch.dict("os.environ", {
            "GMS_OPS_ALERTS_ENABLED": "1",
            "GMS_OPS_ALERTS_MIN_FREE_GB": "42.5",
            "GMS_OPS_ALERTS_CONSECUTIVE_FAILURES": "5",
        }), mock.patch(
            "foundation.config.config_manager",
        ) as config_manager:
            config_manager.load_config.return_value = {}
            cfg = load_ops_alerts_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["min_free_gb"], 42.5)
        self.assertEqual(cfg["consecutive_failure_threshold"], 5)


class WebhookUrlGuardTests(unittest.TestCase):
    """webhook 出站 URL 基础校验（scheme/host/禁重定向）。"""

    def test_plain_http_remote_rejected(self):
        result = send_webhook(
            {"kind": "k"},
            {"webhook_url": "http://10.0.0.5/hook", "webhook_format": "generic",
             "webhook_token_file": ""},
        )
        self.assertFalse(result["sent"])
        self.assertIn("plain http", result["reason"])

    def test_ftp_and_missing_host_rejected(self):
        for url in ("ftp://example/hook", "https:///hook"):
            result = send_webhook(
                {"kind": "k"},
                {"webhook_url": url, "webhook_format": "generic",
                 "webhook_token_file": ""},
            )
            self.assertFalse(result["sent"], url)

    def test_localhost_http_allowed(self):
        class Response:
            status_code = 200

        with mock.patch("httpx.post", return_value=Response()) as post:
            result = send_webhook(
                {"kind": "k"},
                {"webhook_url": "http://127.0.0.1:9000/hook",
                 "webhook_format": "generic", "webhook_token_file": ""},
            )
        self.assertTrue(result["sent"])
        self.assertFalse(post.call_args.kwargs.get("follow_redirects", True))

    def test_bad_numeric_config_falls_back_to_defaults(self):
        with mock.patch.dict("os.environ", {
            "GMS_OPS_ALERTS_SUPPRESS_MINUTES": "not-a-number",
            "GMS_OPS_ALERTS_MIN_FREE_GB": "",
            "GMS_OPS_ALERTS_CONSECUTIVE_FAILURES": "abc",
        }), mock.patch(
            "foundation.config.config_manager",
        ) as config_manager:
            config_manager.load_config.return_value = {}
            cfg = load_ops_alerts_config()
        self.assertEqual(cfg["suppress_minutes"], 60)
        self.assertEqual(cfg["min_free_gb"], 20.0)
        self.assertEqual(cfg["consecutive_failure_threshold"], 3)


if __name__ == "__main__":
    unittest.main()
