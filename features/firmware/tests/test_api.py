import asyncio
import os
import threading
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.firmware import api, firmware_api, runtime, shares_api


class FakeConfigManager:
    def load_config(self):
        return {"ubuntu_user": "tester", "client_username": "codex"}

    def get_ubuntu_user(self, config):
        return config.get("ubuntu_user", "tester")


class FakeSshManager:
    def __init__(self, file_check_output):
        self.file_check_output = file_check_output
        self.commands = []

    def optional_connection(self, _config):
        return self

    @asynccontextmanager
    async def async_optional_connection(self, config):
        with self.optional_connection(config) as ssh:
            yield ssh

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_transport(self):
        return object()

    def execute_command(self, _ssh, cmd, timeout=None):
        self.commands.append((cmd, timeout))
        if "test -f" in cmd:
            return self.file_check_output, "", 0
        return "", "", 0


async def fake_lock_firmware_devices(**_kwargs):
    return ["D1"], None


async def fake_release_firmware_devices(*_args, **_kwargs):
    return None


class FirmwareApiTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = TemporaryDirectory()
        runtime.configure_runtime(
            config_manager=FakeConfigManager(),
            ssh_manager=FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n"),
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
                apk_upload_locks={},
                apk_upload_locks_lock=threading.RLock(),
                firmware_upload_progress={},
                firmware_upload_progress_lock=threading.RLock(),
                websocket_connections={},
            ),
            generate_help_or_continue=lambda help, *_args: (
                JSONResponse({"help": True}) if help else None
            ),
            get_client_id_from_request=lambda _request: "codex@127.0.0.1",
            lock_firmware_devices=fake_lock_firmware_devices,
            release_firmware_devices=fake_release_firmware_devices,
            project_root=".",
            firmware_share_store=None,
            apk_max_tasks=20,
            apk_max_file_size=500 * 1024 * 1024,
            apk_max_source_file_size=2 * 1024 * 1024,
            apk_upload_dir=self.runtime_directory.name,
        )
        app = FastAPI()

        @app.middleware("http")
        async def authenticate_test_request(request, call_next):
            request.state.current_user = CurrentUser(
                id="id-codex", username="codex", role="user"
            )
            return await call_next(request)

        app.include_router(api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.runtime_directory.cleanup()

    def test_firmware_help_does_not_execute_host_command(self):
        response = self.client.post("/api/burn/firmware?help=true")

        self.assertEqual(response.status_code, 200)

    def test_apk_missing_task_returns_not_found(self):
        response = self.client.get("/api/apk/status/missing-task")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_apk_unknown_uuid_task_returns_not_found(self):
        response = self.client.get("/api/apk/status/00000000-0000-0000-0000-000000000000")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_apk_delete_rejects_non_uuid_task_id(self):
        response = self.client.delete("/api/apk/task/not-a-uuid")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_missing_remote_firmware_path_does_not_enter_loader(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        response = self.client.post(
            "/api/burn/firmware?devices=D1",
            data={"firmware_path": "/tmp/not-found.img"},
        )

        self.assertEqual(
            response.json(),
            {"success": False, "error": "Firmware not found: /tmp/not-found.img"},
        )
        self.assertFalse(any("reboot loader" in command for command, _ in fake_ssh.commands))

    def test_invalid_staged_firmware_is_rejected_before_loader(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        with TemporaryDirectory() as tmp, patch(
            "features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT",
            tmp,
        ), patch(
            "features.firmware.firmware_api.validate_local_update_image",
            return_value=firmware_api.FirmwareValidationResult(
                valid=False,
                message="固件预检失败，设备尚未重启：Loader 哈希校验失败。",
            ),
        ):
            session = Path(
                firmware_api._firmware_upload_session_dir(
                    "codex@127.0.0.1",
                    "invalid-loader",
                )
            )
            session.mkdir(parents=True)
            (session / "upload_metadata.json").write_text(
                '{"file_name":"update.img","total_chunks":1,"file_size":8}',
                encoding="utf-8",
            )
            (session / "merged_firmware.bin").write_bytes(b"firmware")

            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"finalize_upload": "1", "upload_id": "invalid-loader"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("设备尚未重启", response.json()["error"])
        self.assertFalse(any("reboot loader" in command for command, _ in fake_ssh.commands))

    def test_gsi_relative_vendor_image_resolves_under_suite_dir(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_FOUND__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        resolved, error = firmware_api._resolve_gsi_remote_image(
            fake_ssh,
            "/home/hcq/GMS-Suite",
            "vendor_boot-debug.img",
            "Vendor boot image",
        )

        self.assertIsNone(error)
        self.assertEqual(resolved, "/home/hcq/GMS-Suite/vendor_boot-debug.img")
        self.assertIn(
            "test -f /home/hcq/GMS-Suite/vendor_boot-debug.img",
            fake_ssh.commands[0][0],
        )

    def test_gsi_missing_relative_vendor_image_returns_error(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        resolved, error = firmware_api._resolve_gsi_remote_image(
            fake_ssh,
            "/home/hcq/GMS-Suite",
            "vendor_boot-debug.img",
            "Vendor boot image",
        )

        self.assertIsNone(resolved)
        self.assertEqual(
            error,
            "Vendor boot image not found: /home/hcq/GMS-Suite/vendor_boot-debug.img",
        )

    def test_gsi_preflight_accepts_adb_and_fastbootd_devices(self):
        class ProtocolSshManager(FakeSshManager):
            def execute_command(self, _ssh, cmd, timeout=None):
                if cmd == "adb devices":
                    return "List of devices attached\nADB001\tdevice\n", "", 0
                if cmd == "fastboot devices":
                    return "FB001\tfastbootd\n", "", 0
                raise AssertionError(cmd)

        runtime.configure_runtime(ssh_manager=ProtocolSshManager(""))
        ready, unavailable = firmware_api._partition_devices_by_flash_state(
            object(),
            ["ADB001", "FB001", "MISSING"],
        )

        self.assertEqual(ready, ["ADB001", "FB001"])
        self.assertEqual(unavailable, ["MISSING"])

    def test_firmware_chunk_upload_can_resume_without_locking_devices(self):
        lock_calls = []

        async def record_lock(**kwargs):
            lock_calls.append(kwargs)
            return ["D1"], None

        runtime.configure_runtime(lock_firmware_devices=record_lock)

        with TemporaryDirectory() as tmp, patch("features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT", tmp):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={
                    "chunk_index": "0",
                    "total_chunks": "2",
                    "upload_id": "upload-1",
                    "file_name": "update.img",
                    "file_size": "8",
                },
                files={"file": ("update.img", b"1234")},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["success"])
            self.assertFalse(payload["upload_complete"])
            self.assertEqual(payload["chunks_uploaded"], 1)
            self.assertEqual(lock_calls, [])

            resume = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={
                    "check_chunks": "1",
                    "total_chunks": "2",
                    "upload_id": "upload-1",
                    "file_name": "update.img",
                    "file_size": "8",
                },
            )
            resume_payload = resume.json()
            self.assertEqual(resume_payload["uploaded_chunks"], [0])
            self.assertEqual(resume_payload["chunks_uploaded"], 1)
            self.assertEqual(resume_payload["total_chunks"], 2)
            self.assertEqual(resume_payload["progress"], 50.0)
            self.assertEqual(resume_payload["uploaded_size"], 4)
            self.assertEqual(resume_payload["total_size"], 8)

    def test_failed_chunk_merge_releases_merge_lock(self):
        async def save_chunk(_upload, path, _max_size=None):
            Path(path).write_bytes(b'1234')

        with (
            TemporaryDirectory() as tmp,
            patch('features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT', tmp),
            patch(
                'features.firmware.chunk_uploads.save_upload_to_path',
                side_effect=save_chunk,
            ),
            patch(
                'features.firmware.chunk_uploads.merge_files_to_path',
                side_effect=OSError('disk full'),
            ),
        ):
            with self.assertRaisesRegex(OSError, 'disk full'):
                asyncio.run(
                    firmware_api._handle_firmware_chunk_upload(
                        {
                            'upload_id': 'failed-merge',
                            'file_name': 'update.img',
                            'chunk_index': '0',
                            'total_chunks': '1',
                            'file_size': '4',
                            'file': object(),
                        },
                        'client',
                    )
                )

            lock_path = firmware_api._firmware_upload_session_dir(
                'client', 'failed-merge'
            )
            self.assertFalse(os.path.exists(os.path.join(lock_path, '.merge.lock')))

    def test_firmware_chunk_upload_rejects_changed_session_metadata(self):
        with TemporaryDirectory() as tmp, patch(
            'features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT',
            tmp,
        ):
            first = self.client.post(
                '/api/burn/firmware?devices=D1',
                data={
                    'chunk_index': '0', 'total_chunks': '2',
                    'upload_id': 'upload-1', 'file_name': 'update.img',
                    'file_size': '8',
                },
                files={'file': ('update.img', b'1234')},
            )
            changed = self.client.post(
                '/api/burn/firmware?devices=D1',
                data={
                    'chunk_index': '1', 'total_chunks': '3',
                    'upload_id': 'upload-1', 'file_name': 'other.img',
                    'file_size': '12',
                },
                files={'file': ('other.img', b'5678')},
            )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(changed.status_code, 400)
            self.assertIn('metadata', changed.json()['error'])

    def test_firmware_chunk_check_rejects_excessive_chunk_count(self):
        with TemporaryDirectory() as tmp, patch(
            'features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT',
            tmp,
        ):
            response = self.client.post(
                '/api/burn/firmware?devices=D1',
                data={
                    'check_chunks': '1',
                    'total_chunks': str(firmware_api.MAX_FIRMWARE_CHUNKS + 1),
                    'upload_id': 'upload-1',
                    'file_name': 'update.img',
                    'file_size': '8',
                },
            )

            self.assertEqual(response.status_code, 400)

    def test_firmware_upload_tokens_do_not_collide_after_sanitizing(self):
        self.assertNotEqual(
            firmware_api._safe_upload_token('client/a'),
            firmware_api._safe_upload_token('client_a'),
        )
        self.assertNotEqual(
            firmware_api._safe_upload_token('x' * 121 + 'a'),
            firmware_api._safe_upload_token('x' * 121 + 'b'),
        )

    def test_expired_firmware_upload_sessions_are_removed(self):
        with TemporaryDirectory() as tmp, patch(
            'features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT',
            tmp,
        ):
            expired = Path(
                firmware_api._firmware_upload_session_dir('client', 'old')
            )
            expired.mkdir(parents=True)
            (expired / 'update.img').write_bytes(b'firmware')
            old = time.time() - firmware_api.UPLOAD_PROGRESS_EXPIRATION - 1
            os.utime(expired, (old, old))

            firmware_api._cleanup_expired_upload_sessions('client')

            self.assertFalse(expired.exists())

    def test_firmware_share_rejects_path_outside_allowed_prefixes(self):
        config = {"firmware_shares": {"allowed_prefixes": ["/home/hcq/"]}}

        with self.assertRaises(ValueError):
            shares_api._validate_remote_path("/etc/passwd", config)

    def test_firmware_share_parses_suffix_range(self):
        self.assertEqual(shares_api._parse_range("bytes=-100", 1000), (900, 999, 100))

    def test_create_firmware_share_persists_remote_record(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            with (
                patch(
                    "features.firmware.shares_api._stat_remote",
                    return_value={
                        "host": "10.10.10.206",
                        "user": "hcq",
                        "path": "/home/hcq/build/Image-rk3576s_u-user",
                        "filename": "Image-rk3576s_u-user",
                        "size": 1234,
                        "mtime": 100,
                    },
                ),
                patch(
                    "features.firmware.shares_api.get_client_username_from_request",
                    return_value="tester",
                ),
            ):
                response = self.client.post(
                    "/api/firmware-shares",
                    json={
                        "remote": "hcq@10.10.10.206:/home/hcq/build/Image-rk3576s_u-user",
                        "name": "daily build",
                    },
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["data"]["record"]["name"], "daily build")
            self.assertEqual(payload["data"]["record"]["size"], 1234)
            self.assertRegex(
                payload["data"]["record"]["id"],
                r"^[0-9a-f]{32}$",
            )
            self.assertNotIn("password", payload["data"]["record"])
            stored = shares_api._load_records()
            self.assertIsNone(stored[0].get("password"))

    def test_create_firmware_share_stores_supplied_password_for_download(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            with (
                patch(
                    "features.firmware.shares_api._stat_remote",
                    return_value={
                        "host": "10.10.10.206",
                        "user": "hcq",
                        "path": "/home/hcq/build/update.img",
                        "filename": "update.img",
                        "size": 1234,
                        "mtime": 100,
                    },
                ),
                patch(
                    "features.firmware.shares_api.get_client_username_from_request",
                    return_value="tester",
                ),
            ):
                self.client.post(
                    "/api/firmware-shares",
                    json={
                        "remote": "hcq@10.10.10.206:/home/hcq/build/update.img",
                        "password": "s3cret",
                    },
                )

            stored = shares_api._load_records()
            self.assertIsNone(stored[0].get("password"))
            self.assertTrue(stored[0].get("password_encrypted"))
            self.assertEqual(
                shares_api._record_password(stored[0]), "s3cret"
            )

    def test_update_firmware_share_credentials_stores_password(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            shares_api._save_records([{
                "id": "share1",
                "name": "update.img",
                "host": "10.10.10.206",
                "user": "hcq",
                "path": "/home/hcq/build/update.img",
                "filename": "update.img",
                "size": 1234,
                "mtime": 100,
            }])
            with patch(
                "features.firmware.shares_api._stat_remote",
                return_value={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build/update.img",
                    "filename": "update.img",
                    "size": 1234,
                    "mtime": 100,
                },
            ) as stat_remote:
                response = self.client.post(
                    "/api/firmware-shares/share1/credentials",
                    json={"password": "fixed"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertIsNone(shares_api._load_records()[0].get("password"))
            self.assertTrue(shares_api._load_records()[0].get("password_encrypted"))
            self.assertEqual(
                shares_api._record_password(shares_api._load_records()[0]), "fixed"
            )
            self.assertEqual(stat_remote.call_args.kwargs["password"], "fixed")

    def test_firmware_share_check_uses_stored_password(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            shares_api._save_records([{
                "id": "share1",
                "name": "update.img",
                "host": "10.10.10.206",
                "user": "hcq",
                "path": "/home/hcq/build/update.img",
                "filename": "update.img",
                "size": 1234,
                "mtime": 100,
                "password": "stored",
            }])
            with patch(
                "features.firmware.shares_api._stat_remote",
                return_value={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build/update.img",
                    "filename": "update.img",
                    "size": 1234,
                    "mtime": 100,
                },
            ) as stat_remote:
                response = self.client.get("/api/firmware-shares/share1/check")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(stat_remote.call_args.kwargs["password"], "stored")

    def test_firmware_share_credentials_use_password_fallback(self):
        creds = shares_api._host_credentials(
            "10.10.10.206",
            "hcq",
            {"ubuntu_host": "10.10.10.206", "ubuntu_pswd": "rockchip"},
        )

        self.assertEqual(creds["username"], "hcq")
        self.assertEqual(creds["password"], "rockchip")

    def test_firmware_share_does_not_send_ubuntu_password_to_other_host(self):
        creds = shares_api._host_credentials(
            'attacker.invalid',
            'hcq',
            {'ubuntu_host': '10.10.10.206', 'ubuntu_pswd': 'rockchip'},
        )

        self.assertIsNone(creds['password'])

    def test_firmware_share_browse_uses_remote_directory_listing(self):
        with (
            patch(
                "features.firmware.shares_api._list_remote_dir",
                return_value={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build",
                    "files": [{"name": "Image-rk3576s_u-userdebug", "type": "file", "size": 10}],
                },
            ),
        ):
            response = self.client.post(
                "/api/firmware-shares/browse",
                json={"host": "10.10.10.206", "user": "hcq", "path": "/home/hcq/build"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["files"][0]["name"], "Image-rk3576s_u-userdebug")

    def test_firmware_share_browse_auth_failure_returns_401(self):
        with patch(
            "features.firmware.shares_api._list_remote_dir",
            side_effect=shares_api._AuthError("远端认证失败（用户名/密码/密钥不匹配）"),
        ):
            response = self.client.post(
                "/api/firmware-shares/browse",
                json={"host": "10.10.10.206", "user": "hcq", "path": "/home/hcq/build"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["success"])

    def test_firmware_share_browse_forwards_password(self):
        captured = {}

        def _spy(host, user, path, config, password=None):
            captured["password"] = password
            return {"host": host, "user": user, "path": path, "files": []}

        with patch("features.firmware.shares_api._list_remote_dir", side_effect=_spy):
            self.client.post(
                "/api/firmware-shares/browse",
                json={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build",
                    "password": "s3cret",
                },
            )

        self.assertEqual(captured["password"], "s3cret")

    def test_sftp_client_prefers_supplied_password(self):
        captured = {}

        class _FakeSftp:
            def stat(self, path):
                captured["called"] = True

            def close(self):
                pass

        class _FakeClient:
            def __init__(self):
                self._connect_kwargs = None

            def set_missing_host_key_policy(self, policy):
                pass

            def load_system_host_keys(self):
                pass

            def load_host_keys(self, path):
                pass

            def connect(self, **kwargs):
                self._connect_kwargs = kwargs
                captured["password"] = kwargs.get("password")

            def open_sftp(self):
                return _FakeSftp()

            def close(self):
                pass

        with (
            patch("features.firmware.shares_api.paramiko.SSHClient", return_value=_FakeClient()),
            patch("features.firmware.shares_api._host_credentials", return_value={
                "hostname": "10.10.10.206", "username": "hcq", "password": None,
                "key_filename": None, "port": 22,
            }),shares_api._sftp_client("10.10.10.206", "hcq", {}, password="override")
        ):
            pass

        self.assertEqual(captured["password"], "override")
