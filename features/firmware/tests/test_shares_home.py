from __future__ import annotations

import asyncio
import json
import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware import runtime, shares_api


class FirmwareShareHomeTests(unittest.TestCase):
    def test_credentials_use_exact_host_scoped_password(self):
        manager = SimpleNamespace(
            find_device_host_password=lambda device_host, _config: (
                "saved-secret"
                if device_host == "hcq@172.16.14.66"
                else None
            ),
        )
        runtime.configure_runtime(config_manager=manager)

        creds = shares_api._host_credentials(
            "172.16.14.66",
            "hcq",
            {"ubuntu_host": "172.16.14.233", "ubuntu_pswd": "legacy"},
        )

        self.assertEqual(creds["username"], "hcq")
        self.assertEqual(creds["password"], "saved-secret")

    def test_firmware_specific_password_precedes_host_scoped_password(self):
        manager = SimpleNamespace(
            find_device_host_password=lambda _device_host, _config: "saved-secret",
        )
        runtime.configure_runtime(config_manager=manager)

        creds = shares_api._host_credentials(
            "172.16.14.66",
            "hcq",
            {
                "firmware_shares": {
                    "hosts": {
                        "172.16.14.66": {"password": "firmware-secret"},
                    },
                },
            },
        )

        self.assertEqual(creds["password"], "firmware-secret")

    def test_host_password_can_come_from_configured_environment(self):
        manager = SimpleNamespace(
            find_device_host_password=lambda _device_host, _config: None,
        )
        runtime.configure_runtime(config_manager=manager)
        config = {
            "firmware_shares": {
                "hosts": {
                    "10.10.10.206": {
                        "password_env": "RK_BUILD_206_PASSWORD",
                    },
                },
            },
        }

        with patch.dict(
            os.environ,
            {"RK_BUILD_206_PASSWORD": "build-secret"},
        ):
            creds = shares_api._host_credentials(
                "10.10.10.206",
                "hcq",
                config,
            )

        self.assertEqual(creds["password"], "build-secret")

    def test_empty_path_uses_sftp_home_and_allows_its_children(self):
        listed_paths = []

        class FakeSftp:
            def normalize(self, path):
                self_test.assertEqual(path, ".")
                return "/C:/Users/hcq"

            def listdir_attr(self, path):
                listed_paths.append(path)
                return [
                    SimpleNamespace(
                        filename="firmware",
                        st_mode=0o040755,
                        st_size=0,
                        st_mtime=100,
                    ),
                ]

        self_test = self

        @contextmanager
        def fake_client(*_args, **_kwargs):
            yield FakeSftp(), {"username": "hcq"}

        with patch(
            "features.firmware.shares_api._sftp_client",
            side_effect=fake_client,
        ):
            home = shares_api._list_remote_dir(
                "172.16.14.66", "hcq", "", {},
            )
            child = shares_api._list_remote_dir(
                "172.16.14.66", "hcq", "/C:/Users/hcq/firmware", {},
            )

        self.assertEqual(home["path"], "/C:/Users/hcq")
        self.assertEqual(home["files"][0]["name"], "firmware")
        self.assertEqual(
            listed_paths,
            ["/C:/Users/hcq", "/C:/Users/hcq/firmware"],
        )
        self.assertEqual(child["path"], "/C:/Users/hcq/firmware")

    def test_sftp_home_does_not_authorize_paths_outside_home(self):
        class FakeSftp:
            def normalize(self, _path):
                return "/C:/Users/hcq"

            def listdir_attr(self, _path):
                raise AssertionError("outside path must be rejected before listing")

        @contextmanager
        def fake_client(*_args, **_kwargs):
            yield FakeSftp(), {"username": "hcq"}

        with patch(
            "features.firmware.shares_api._sftp_client",
            side_effect=fake_client,
        ):
            with self.assertRaisesRegex(ValueError, "不在允许范围内"):
                shares_api._list_remote_dir(
                    "172.16.14.66", "hcq", "/C:/Windows", {},
                )

    def test_browse_without_configured_path_forwards_empty_path(self):
        captured = {}

        class FakeRequest:
            async def json(self):
                return {"host": "172.16.14.66", "user": "hcq"}

        manager = SimpleNamespace(
            load_config=lambda: {
                "local_server": "hcq@172.16.14.66",
                "ubuntu_user": "hcq",
            },
        )
        runtime.configure_runtime(config_manager=manager)

        def fake_list(host, user, path, _config, password=None):
            captured.update(
                host=host,
                user=user,
                path=path,
                password=password,
            )
            return {
                "host": host,
                "user": user,
                "path": "/C:/Users/hcq",
                "files": [],
            }

        with patch(
            "features.firmware.shares_api._list_remote_dir",
            side_effect=fake_list,
        ):
            response = asyncio.run(
                shares_api.browse_firmware_share_remote(FakeRequest())
            )

        self.assertEqual(captured["path"], "")
        payload = json.loads(response.body)
        self.assertEqual(payload["data"]["path"], "/C:/Users/hcq")


if __name__ == "__main__":
    unittest.main()
