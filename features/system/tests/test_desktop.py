import unittest

from features.system.desktop import (
    build_novnc_upstream_url,
    default_novnc_upstream_http,
    novnc_locale_override,
)


class DesktopProxyTests(unittest.TestCase):
    def test_default_novnc_upstream_uses_shared_web_port(self):
        self.assertEqual(default_novnc_upstream_http(), "http://127.0.0.1:6080")

    def test_build_novnc_upstream_url_normalizes_path_and_query(self):
        self.assertEqual(
            build_novnc_upstream_url("/vnc.html", b"autoconnect=true"),
            "http://127.0.0.1:6080/vnc.html?autoconnect=true",
        )

    def test_novnc_zh_locale_is_overridden_with_simplified_chinese(self):
        locale = novnc_locale_override("/app/locale/zh.json")

        self.assertIsNotNone(locale)
        self.assertIn("无法连接到服务器".encode(), locale)
        self.assertNotIn("無法連線到伺服器".encode(), locale)
        self.assertIsNone(novnc_locale_override("app/ui.js"))


if __name__ == "__main__":
    unittest.main()
