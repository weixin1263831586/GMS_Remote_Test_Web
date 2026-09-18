from __future__ import annotations

import asyncio
import json
import os
import unittest
from contextlib import contextmanager
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware import runtime, shares_api


class FirmwareShareHomeTests(unittest.TestCase):
    def test_rejects_path_outside_allowed_prefixes(self):
        config = {"firmware_shares": {"allowed_prefixes": ["/home/hcq/"]}}

        with self.assertRaises(ValueError):
            shares_api._validate_remote_path("/etc/passwd", config)

    def test_parses_suffix_range(self):
        self.assertEqual(
            shares_api._parse_range("bytes=-100", 1000),
            (900, 999, 100),
        )

    def test_credentials_use_password_fallback_for_exact_host(self):
        creds = shares_api._host_credentials(
            "10.10.10.206",
            "hcq",
            {"ubuntu_host": "10.10.10.206", "ubuntu_pswd": "rockchip"},
        )

        self.assertEqual(creds["username"], "hcq")
        self.assertEqual(creds["password"], "rockchip")

    def test_does_not_send_ubuntu_password_to_other_host(self):
        creds = shares_api._host_credentials(
            "attacker.invalid",
            "hcq",
            {"ubuntu_host": "10.10.10.206", "ubuntu_pswd": "rockchip"},
        )

        self.assertIsNone(creds["password"])

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
                # 真实 SFTP 服务端对 "." 返回 HOME，对其余入参返回
                # realpath 归一化结果（这里用原样返回模拟）。
                if path == ".":
                    self_test.assertEqual(path, ".")
                    return "/C:/Users/hcq"
                return str(PurePosixPath(path))

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
            def normalize(self, path):
                if path == ".":
                    return "/C:/Users/hcq"
                return str(PurePosixPath(path))

            def listdir_attr(self, _path):
                raise AssertionError("outside path must be rejected before listing")

        @contextmanager
        def fake_client(*_args, **_kwargs):
            yield FakeSftp(), {"username": "hcq"}

        with patch(
            "features.firmware.shares_api._sftp_client",
            side_effect=fake_client,
        ), self.assertRaisesRegex(ValueError, "不在允许范围内"):
            shares_api._list_remote_dir(
                "172.16.14.66", "hcq", "/C:/Windows", {},
            )

    def test_symlink_escaping_allowed_root_is_rejected_on_stat(self):
        # 共享目录里放置指向 allowed root 之外的 symlink：词法校验看到的是
        # 合法路径，但服务端 realpath 会暴露真实目标，必须拒绝。
        class FakeSftp:
            def normalize(self, path):
                if path == ".":
                    return "/home/hcq"
                return {
                    "/home/hcq/payload.bin": "/etc/shadow",
                }.get(str(PurePosixPath(path)), str(PurePosixPath(path)))

            def lstat(self, _path):
                return SimpleNamespace(st_mode=0o120777)

            def stat(self, _path):
                raise AssertionError("symlink escape must be rejected before stat")

        @contextmanager
        def fake_client(*_args, **_kwargs):
            yield FakeSftp(), {"username": "hcq"}

        with patch(
            "features.firmware.shares_api._sftp_client",
            side_effect=fake_client,
        ), self.assertRaisesRegex(ValueError, "不在允许范围内"):
            shares_api._stat_remote(
                "172.16.14.66", "hcq", "/home/hcq/payload.bin", {},
            )

    def test_symlink_leaf_inside_allowed_root_is_rejected(self):
        class FakeSftp:
            def normalize(self, path):
                if path == ".":
                    return "/home/hcq"
                return str(PurePosixPath(path))

            def lstat(self, _path):
                return SimpleNamespace(st_mode=0o120777)

            def stat(self, _path):
                raise AssertionError("symlink leaf must be rejected before stat")

        @contextmanager
        def fake_client(*_args, **_kwargs):
            yield FakeSftp(), {"username": "hcq"}

        with patch(
            "features.firmware.shares_api._sftp_client",
            side_effect=fake_client,
        ), self.assertRaisesRegex(ValueError, "符号链接"):
            shares_api._stat_remote(
                "172.16.14.66", "hcq", "/home/hcq/link.bin", {},
            )

    def test_stat_persists_canonical_resolved_path(self):
        class FakeSftp:
            def normalize(self, path):
                if path == ".":
                    return "/home/hcq"
                return {
                    "/home/hcq/./firmware.bin": "/home/hcq/firmware.bin",
                }.get(str(PurePosixPath(path)), str(PurePosixPath(path)))

            def lstat(self, _path):
                return SimpleNamespace(st_mode=0o100644)

            def stat(self, _path):
                return SimpleNamespace(st_size=4096, st_mtime=123)

        @contextmanager
        def fake_client(*_args, **_kwargs):
            yield FakeSftp(), {"username": "hcq"}

        with patch(
            "features.firmware.shares_api._sftp_client",
            side_effect=fake_client,
        ):
            info = shares_api._stat_remote(
                "172.16.14.66", "hcq", "/home/hcq/./firmware.bin", {},
            )

        self.assertEqual(info["path"], "/home/hcq/firmware.bin")

    def test_download_iterator_revalidates_within_its_own_session(self):
        opened_paths = []

        class FakeFile:
            def seek(self, _offset):
                pass

            def read(self, _size):
                return b""

        class FakeSftp:
            def normalize(self, path):
                if path == ".":
                    return "/home/hcq"
                return {
                    "/home/hcq/payload.bin": "/etc/shadow",
                }.get(str(PurePosixPath(path)), str(PurePosixPath(path)))

            def open(self, path, _mode):
                opened_paths.append(path)
                return FakeFile()

        @contextmanager
        def fake_client(*_args, **_kwargs):
            yield FakeSftp(), {"username": "hcq"}

        with patch(
            "features.firmware.shares_api._sftp_client",
            side_effect=fake_client,
        ), self.assertRaisesRegex(ValueError, "不在允许范围内"):
            list(shares_api._remote_file_iterator(
                "172.16.14.66", "hcq", "/home/hcq/payload.bin", {}, 0, 10,
            ))

        self.assertEqual(opened_paths, [])

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
