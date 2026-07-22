import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.system import assets


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
                    return_value=(output, "", 0),
                ) as execute:
            response = asyncio.run(assets.list_files({"path": "~/GMS-Suite"}, None))

        self.assertEqual(response.status_code, 200)
        execute.assert_called_once_with(
            ssh,
            "ls -la -- /home/tester/GMS-Suite 2>/dev/null",
        )


if __name__ == "__main__":
    unittest.main()
