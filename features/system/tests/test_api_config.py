import asyncio
import os
import unittest
from unittest.mock import mock_open, patch

from features.system import api


class SystemApiConfigTests(unittest.TestCase):
    def test_gms_assistant_upstream_uses_product_config(self):
        config = {"external_services": {"gms_assistant_url": "http://assistant.internal/"}}
        with patch.dict(os.environ, {}, clear=True), patch.object(
            api.config_manager, "load_config", return_value=config
        ):
            self.assertEqual(api._gms_assistant_upstream(), "http://assistant.internal")

    def test_gms_assistant_environment_override_wins(self):
        with patch.dict(
            os.environ, {"GMS_ASSISTANT_URL": "https://assistant.example/"}, clear=True
        ):
            self.assertEqual(api._gms_assistant_upstream(), "https://assistant.example")

    def test_gms_assistant_api_key_prefers_environment(self):
        with patch.dict(
            os.environ, {"GMS_ASSISTANT_API_KEY": "env-secret"}, clear=True
        ), patch.object(
            api.config_manager, "load_config",
            return_value={"external_services": {"gms_assistant_api_key": "config-secret"}},
        ):
            self.assertEqual(api._gms_assistant_api_key(), "env-secret")

    def test_gms_assistant_api_key_falls_back_to_config(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            api.config_manager, "load_config",
            return_value={"external_services": {"gms_assistant_api_key": "config-secret"}},
        ):
            self.assertEqual(api._gms_assistant_api_key(), "config-secret")

    def test_gms_assistant_proxy_removes_external_google_font_stylesheet(self):
        source = """<html><head>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap"
              rel="stylesheet">
        </head><body>chat</body></html>"""

        rewritten = api._rewrite_gms_assistant_content(
            source,
            None,
            proxy_base="/gms-assistant",
            upstream="http://assistant.internal",
        )

        self.assertNotIn("fonts.googleapis.com", rewritten)
        self.assertNotIn("fonts.gstatic.com", rewritten)
        self.assertIn("<body>chat</body>", rewritten)

    def test_architecture_replaces_configured_build_server_safely(self):
        source = "<text>{{BUILD_SERVER_LABEL}} • 编译服务器</text>"
        config = {"ui_defaults": {"architecture_build_server": "build<primary>"}}
        with patch.object(api.os.path, "exists", return_value=True), patch(
            "builtins.open", mock_open(read_data=source)
        ), patch.object(api.config_manager, "load_config", return_value=config):
            response = asyncio.run(api.get_architecture())

        self.assertIn("build&lt;primary&gt;", response.body.decode("utf-8"))

    def test_shell_titles_cover_cluster_and_knowledge_pages(self):
        self.assertIn("cluster", api.SHELL_PAGE_TITLES)
        self.assertIn("notes", api.SHELL_PAGE_TITLES)


if __name__ == "__main__":
    unittest.main()
