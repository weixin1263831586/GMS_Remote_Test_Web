"""固件烧写错误语义回归：upgrade_tool 执行失败必须按 502 UPSTREAM_FAILURE 返回。

历史上该路径错误地返回 422，把基础设施故障伪装成参数错误，误导前端与
Agent 按“修正参数”而非“重试/检查 USB”处理（全局错误码表：422 只保留给
前置参数校验）。本文件只承载烧写失败语义，fixture 与 ``test_api.py`` 保持
独立，避免继续膨胀该文件。
"""

import contextlib
import threading
import unittest
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.firmware import api, firmware_api, runtime
from foundation.command_result import CommandResult


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
            return CommandResult(
                stdout=self.file_check_output, stderr="", code=0,
            )
        return CommandResult(stdout="", stderr="", code=0)


async def fake_lock_firmware_devices(**_kwargs):
    return ["D1"], None


async def fake_release_firmware_devices(*_args, **_kwargs):
    return None


class BurnErrorSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = TemporaryDirectory()
        runtime.configure_runtime(
            config_manager=FakeConfigManager(),
            ssh_manager=FakeSshManager("__GMS_REMOTE_FILE_FOUND__\n"),
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

    def test_upgrade_tool_burn_failure_maps_to_upstream_502(self):
        """烧写执行失败（upgrade_tool 输出 Download Firmware Fail）按 502 语义返回。"""
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_FOUND__\n")
        runtime.configure_runtime(
            ssh_manager=fake_ssh,
            store_notification=lambda *args, **kwargs: None,
        )
        burn_error = (
            "Download Image... (4%) Download Firmware Fail "
            "Note:Communication issues"
        )

        async def fake_batch(**_kwargs):
            return [{
                "device": "D1", "success": False, "stage": "FLASHING",
                "exit_code": 1, "error": burn_error,
            }], burn_error

        async def fake_usbip_routes(_devices):
            return [], None

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(
                firmware_api, "_prepare_usbip_firmware_routes",
                new=fake_usbip_routes,
            ))
            stack.enter_context(patch.object(
                firmware_api, "_device_flash_protocols",
                new=lambda *_args: {"D1": "rockusb-loader"},
            ))
            stack.enter_context(patch.object(
                firmware_api, "_run_local_firmware_batch", new=fake_batch,
            ))
            stack.enter_context(patch.object(
                firmware_api, "validate_remote_update_image",
                new=lambda *_args: SimpleNamespace(valid=True, message=""),
            ))
            stack.enter_context(patch("scp.SCPClient"))
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/tmp/update.img"},
            )

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["code"], "UPSTREAM_FAILURE")
        self.assertFalse(body["success"])
        self.assertIn("Download Firmware Fail", body["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
