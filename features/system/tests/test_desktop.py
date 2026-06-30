import unittest

from features.system.desktop import build_novnc_upstream_url, default_novnc_upstream_http


class DesktopProxyTests(unittest.TestCase):
    def test_default_novnc_upstream_uses_shared_web_port(self):
        self.assertEqual(default_novnc_upstream_http(), "http://127.0.0.1:6080")

    def test_build_novnc_upstream_url_normalizes_path_and_query(self):
        self.assertEqual(
            build_novnc_upstream_url("/vnc.html", b"autoconnect=true"),
            "http://127.0.0.1:6080/vnc.html?autoconnect=true",
        )


if __name__ == "__main__":
    unittest.main()
