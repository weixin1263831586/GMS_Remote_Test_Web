import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.system import assets
from features.system.favicon_security import FaviconResolver
from features.system.icon_fetcher import IconFetcher
from foundation.command_result import CommandResult
from foundation.outbound import ResolvedOutboundTarget


class FileListingTests(unittest.TestCase):
    def test_local_host_uses_filesystem_without_ssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "GMS Suite"
            root.mkdir()
            (root / "images").mkdir()
            (root / "system image.img").write_bytes(b"image")
            config = {"ubuntu_host": "127.0.0.1", "ubuntu_user": "tester"}

            with patch.object(assets.config_manager, "load_config", return_value=config), \
                    patch.object(assets.config_manager, "get_ubuntu_user", return_value="tester"), \
                    patch.object(assets.config_manager, "is_config_host_local", return_value=True), \
                    patch.object(
                        assets.ssh_manager,
                        "optional_connection",
                        side_effect=AssertionError("local listing must not use SSH"),
                    ):
                response = asyncio.run(assets.list_files({"path": str(root)}, None))

            payload = json.loads(response.body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["path"], str(root))
            self.assertEqual(
                [(item["name"], item["type"]) for item in payload["files"]],
                [("images", "directory"), ("system image.img", "file")],
            )

    def test_local_default_expands_configured_suite_home(self):
        config = {
            "ubuntu_host": "localhost",
            "ubuntu_user": "tester",
            "suites_path": "~/GMS-Suite",
        }
        with patch.object(assets.config_manager, "load_config", return_value=config), \
                patch.object(assets.config_manager, "get_ubuntu_user", return_value="tester"), \
                patch.object(assets.config_manager, "is_config_host_local", return_value=True), \
                patch.object(assets, "_list_local_files", return_value=[]) as listing:
            response = asyncio.run(assets.list_files({}, None))

        self.assertEqual(response.status_code, 200)
        listing.assert_called_once_with("/home/tester/GMS-Suite")

    def test_remote_host_keeps_ssh_listing(self):
        config = {"ubuntu_host": "192.0.2.10", "ubuntu_user": "tester"}
        ssh = object()
        output = "drwxr-xr-x 2 tester tester 4096 Jul 20 12:00 images\n"
        with patch.object(assets.config_manager, "load_config", return_value=config), \
                patch.object(assets.config_manager, "get_ubuntu_user", return_value="tester"), \
                patch.object(assets.config_manager, "is_config_host_local", return_value=False), \
                patch.object(
                    assets.ssh_manager,
                    "optional_connection",
                    return_value=contextlib.nullcontext(ssh),
                ), patch.object(
                    assets.ssh_manager,
                    "execute_command",
                    return_value=CommandResult(stdout=output, stderr="", code=0),
                ) as execute:
            response = asyncio.run(assets.list_files({"path": "~/GMS-Suite"}, None))

        self.assertEqual(response.status_code, 200)
        execute.assert_called_once_with(
            ssh,
            "ls -la -- /home/tester/GMS-Suite 2>/dev/null",
        )


class FaviconSecurityTests(unittest.TestCase):
    def test_static_url_to_path_rejects_path_traversal(self):
        path = IconFetcher.static_url_to_path(
            "/static/icons/favicons/../../../../configs/config.json"
        )

        self.assertEqual(path, "")

    def test_private_favicon_target_is_rejected_without_allowlist(self):
        fetcher = IconFetcher(timeout=1)
        try:
            result = asyncio.run(
                fetcher.localize_icon_url("http://127.0.0.1/favicon.ico")
            )
        finally:
            asyncio.run(fetcher.close())

        self.assertFalse(result.success)
        self.assertIn("Private or reserved", result.error)

    def test_svg_is_active_content_not_a_safe_image(self):
        payload = b'<?xml version="1.0"?><svg><script>alert(1)</script></svg>'

        self.assertTrue(IconFetcher._is_svg_content(payload))
        self.assertFalse(IconFetcher._is_image_content(payload))

    def test_favicon_resolver_pins_validated_address(self):
        target = ResolvedOutboundTarget(
            url="https://example.com/favicon.ico",
            hostname="example.com",
            port=443,
            addresses=("93.184.216.34",),
        )
        with patch(
            "features.system.favicon_security.resolve_outbound_target",
            return_value=target,
        ):
            resolved = asyncio.run(FaviconResolver().resolve("example.com", 443))

        self.assertEqual(resolved[0]["host"], "93.184.216.34")
        self.assertEqual(resolved[0]["hostname"], "example.com")


class WebsiteToolsValidationTests(unittest.TestCase):
    def test_accepts_https_relative_and_legacy_bare_host_urls(self):
        tools = {
            "Development": [
                {"title": "Docs", "url": "https://example.com/docs", "icon": "📘"},
                {"title": "Reports", "url": "/reports", "icon": "📊"},
                {"title": "Legacy", "url": "example.org", "icon": ""},
            ]
        }

        assets._validate_tools_data(tools)

    def test_rejects_script_protocol(self):
        tools = {"Bad": [{"title": "Unsafe", "url": "javascript:alert(1)", "icon": ""}]}

        with self.assertRaisesRegex(ValueError, "Unsupported"):
            assets._validate_tools_data(tools)

    def test_rejects_protocol_relative_url(self):
        tools = {"Bad": [{"title": "Unsafe", "url": "//example.com", "icon": ""}]}

        with self.assertRaisesRegex(ValueError, "Invalid"):
            assets._validate_tools_data(tools)


if __name__ == "__main__":
    unittest.main()
