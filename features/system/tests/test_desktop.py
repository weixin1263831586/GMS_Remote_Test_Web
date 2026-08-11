import unittest
from types import SimpleNamespace

from features.system.desktop import (
    build_novnc_upstream_url,
    create_novnc_client_session,
    default_novnc_upstream_http,
    novnc_asset_override,
    novnc_client_session,
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

    def test_novnc_clipboard_falls_back_when_iframe_is_not_focused(self):
        entry = novnc_asset_override(
            "vnc.html",
            b'import UI from "./app/ui.js";',
        )
        ui = novnc_asset_override(
            "app/ui.js",
            b'import RFB from "../core/rfb.js";',
        )
        rfb = novnc_asset_override(
            "core/rfb.js",
            b'import AsyncClipboard from "./clipboard.js";',
        )
        clipboard = novnc_asset_override(
            "core/clipboard.js",
            b"if (!this._isAvailable) return false;",
        )

        self.assertIn(b"gms_asset=20260718-clipboard-focus", entry)
        self.assertIn(b"gms_asset=20260718-clipboard-focus", ui)
        self.assertIn(b"gms_asset=20260718-clipboard-focus", rfb)
        self.assertIn(b"!document.hasFocus()", clipboard)


class DesktopProxyPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_session_is_reused_without_being_closed_by_request(self):
        session = create_novnc_client_session()
        connection = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(novnc_client_session=session)
            )
        )
        try:
            async with (
                novnc_client_session(connection) as first,
                novnc_client_session(connection) as second,
            ):
                self.assertIs(first, session)
                self.assertIs(second, session)
            self.assertFalse(session.closed)
        finally:
            await session.close()


if __name__ == "__main__":
    unittest.main()
