"""Partner docs auth-failure detection and host Firefox auto-launch."""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from foundation import partner_android


class IsAuthFailureTests(unittest.TestCase):
    def test_login_redirect_counts_regardless_of_status(self):
        self.assertTrue(partner_android.is_auth_failure(
            200, "https://accounts.google.com/ServiceLogin?continue=x"))

    def test_auth_statuses_count(self):
        for status in (401, 403):
            self.assertTrue(partner_android.is_auth_failure(
                status, "https://docs.partner.android.com/x"))

    def test_partner_404_counts_by_default(self):
        self.assertTrue(partner_android.is_auth_failure(
            404, "https://docs.partner.android.com/mainline/release/release-notes"))

    def test_partner_404_ignored_when_allow_404_false(self):
        self.assertFalse(partner_android.is_auth_failure(
            404, "https://docs.partner.android.com/gone-page", allow_404=False))

    def test_unrelated_host_404_does_not_count(self):
        self.assertFalse(partner_android.is_auth_failure(
            404, "https://source.android.com/docs/compatibility/cts/downloads"))

    def test_success_on_partner_host_does_not_count(self):
        self.assertFalse(partner_android.is_auth_failure(
            200, "https://docs.partner.android.com/gms/testing/vts"))


class LaunchLoginBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = str(Path(self.tmp.name) / "launch-marker")
        env = {
            "GMS_PARTNER_ANDROID_AUTO_LOGIN_MARKER": self.marker,
            "GMS_PARTNER_AUTO_LOGIN_DISPLAY": ":0",
        }
        env_patcher = patch.dict(os.environ, env)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_launches_firefox_on_host_display(self):
        # 自愈弹窗默认关闭，需要显式开启（GMS_PARTNER_ANDROID_AUTO_LOGIN=1）。
        with patch.dict(os.environ, {"GMS_PARTNER_ANDROID_AUTO_LOGIN": "1"}), \
                patch("foundation.partner_android.shutil.which", return_value="/usr/bin/firefox"), \
                patch("foundation.partner_android.subprocess.Popen") as popen:
            launched = partner_android.launch_login_browser(
                "https://docs.partner.android.com/x", reason="test", verbose=False)

        self.assertTrue(launched)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/usr/bin/firefox", "https://docs.partner.android.com/x"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["DISPLAY"], ":0")
        self.assertTrue(Path(self.marker).exists())

    def test_disabled_by_default(self):
        # 未设置环境变量时必须不弹窗：服务端不应擅自打开桌面浏览器。
        with patch("foundation.partner_android.shutil.which", return_value="/usr/bin/firefox"), \
                patch("foundation.partner_android.subprocess.Popen") as popen:
            launched = partner_android.launch_login_browser("https://docs.partner.android.com/x")

        self.assertFalse(launched)
        popen.assert_not_called()

    def test_debounces_recent_launch(self):
        Path(self.marker).write_text(f"{int(time.time())}\tbefore\n", encoding="utf-8")
        with patch("foundation.partner_android.shutil.which", return_value="/usr/bin/firefox"), \
                patch("foundation.partner_android.subprocess.Popen") as popen:
            launched = partner_android.launch_login_browser("https://docs.partner.android.com/x")

        self.assertFalse(launched)
        popen.assert_not_called()

    def test_disabled_by_env(self):
        with patch.dict(os.environ, {"GMS_PARTNER_ANDROID_AUTO_LOGIN": "0"}), \
                patch("foundation.partner_android.shutil.which", return_value="/usr/bin/firefox"), \
                patch("foundation.partner_android.subprocess.Popen") as popen:
            launched = partner_android.launch_login_browser("https://docs.partner.android.com/x")

        self.assertFalse(launched)
        popen.assert_not_called()

    def test_enabled_by_env_truthy_value(self):
        with patch.dict(os.environ, {"GMS_PARTNER_ANDROID_AUTO_LOGIN": "true"}), \
                patch("foundation.partner_android.shutil.which", return_value="/usr/bin/firefox"), \
                patch("foundation.partner_android.subprocess.Popen") as popen:
            launched = partner_android.launch_login_browser("https://docs.partner.android.com/x")

        self.assertTrue(launched)
        popen.assert_called_once()

    def test_skips_when_display_socket_missing(self):
        # :99 没有 X socket（本机只有 gdm 的 :0），必须拒绝弹窗。
        with patch.dict(os.environ, {"GMS_PARTNER_AUTO_LOGIN_DISPLAY": ":99"}), \
                patch("foundation.partner_android.shutil.which", return_value="/usr/bin/firefox"), \
                patch("foundation.partner_android.subprocess.Popen") as popen:
            launched = partner_android.launch_login_browser("https://docs.partner.android.com/x")

        self.assertFalse(launched)
        popen.assert_not_called()

    def test_skips_when_firefox_missing(self):
        with patch("foundation.partner_android.shutil.which", return_value=None), \
                patch("foundation.partner_android.subprocess.Popen") as popen:
            launched = partner_android.launch_login_browser("https://docs.partner.android.com/x")

        self.assertFalse(launched)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
