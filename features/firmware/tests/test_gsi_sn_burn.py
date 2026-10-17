"""burn_gsi 锁与执行边界回归。

覆盖 2026-10-09 全库审计的三个 P1/P2 项：
1. 锁获取后的 USB/IP 路由预检抛异常时必须释放设备锁（不再占用到锁 TTL）；
2. partition_devices_by_flash_state 抛出的 ApiError(502) 原样返回信封，
   不被兜底 except 吞成 500；
3. 烧写执行经 ssh_executor.run_stream 的 deadline 收口：远端 fastboot
   卡死时按 timed_out 返回失败结果，请求不会永挂。
"""

import contextlib
import threading
import unittest
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.firmware import api, gsi_sn_burn, runtime
from foundation.command_result import CommandResult
from foundation.error_model import ApiError


class FakeConfigManager:
    def load_config(self):
        return {"ubuntu_user": "tester", "client_username": "codex"}


class FakeSshManager:
    @asynccontextmanager
    async def async_optional_connection(self, _config):
        yield self

    def execute_command(self, _ssh, _cmd, timeout=None):
        return CommandResult(stdout="", stderr="", code=0)


class LockTracker:
    def __init__(self):
        self.locked: list[tuple] = []
        self.released: list[tuple] = []

    async def lock(self, *, client_id, devices, **_kwargs):
        self.locked.append((client_id, list(devices)))
        return list(devices), None

    async def release(self, client_id, devices):
        self.released.append((client_id, list(devices)))


def _ok_partition(_ssh, devices):
    return list(devices), []


async def _noop_async(*_args, **_kwargs):
    return None


async def _ok_usbip_routes(_devices):
    return [], None


class BurnGsiLockAndDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.locks = LockTracker()
        self.runtime_directory = TemporaryDirectory()
        runtime.configure_runtime(
            config_manager=FakeConfigManager(),
            ssh_manager=FakeSshManager(),
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
                apk_upload_locks={},
                apk_upload_locks_lock=threading.RLock(),
                firmware_upload_progress={},
                firmware_upload_progress_lock=threading.RLock(),
                websocket_connections={},
            ),
            generate_help_or_continue=lambda help, *_args: None,
            get_client_id_from_request=lambda _request: "codex@127.0.0.1",
            lock_firmware_devices=self.locks.lock,
            release_firmware_devices=self.locks.release,
            project_root=".",
            firmware_share_store=None,
            apk_max_tasks=20,
            apk_max_file_size=500 * 1024 * 1024,
            apk_max_source_file_size=2 * 1024 * 1024,
            apk_upload_dir=self.runtime_directory.name,
            store_notification=lambda *args, **kwargs: None,
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

    def _post_burn(self):
        return self.client.post(
            "/api/burn/gsi",
            json={
                "devices": ["D1"],
                "script_path": "/tmp/run_GSI_Burn.sh",
                "system_img": "/tmp/system.img",
            },
        )

    def _patch_common(self, stack, *, usbip_routes=None, partition=None):
        stack.enter_context(patch.object(
            gsi_sn_burn, "_partition_devices_by_flash_state",
            new=partition or _ok_partition,
        ))
        stack.enter_context(patch.object(
            gsi_sn_burn, "_notify_skip", new=_noop_async,
        ))
        stack.enter_context(patch.object(
            gsi_sn_burn, "_prepare_usbip_firmware_routes",
            new=usbip_routes or _ok_usbip_routes,
        ))
        stack.enter_context(patch.object(
            gsi_sn_burn, "get_default_suites_path",
            new=lambda _config: "/suites",
        ))
        stack.enter_context(patch.object(
            gsi_sn_burn, "upload_gsi_assets",
            new=lambda **_kwargs: ("/remote/run.sh", "/remote/misc.img"),
        ))
        stack.enter_context(patch.object(
            gsi_sn_burn, "_resolve_gsi_remote_image",
            new=lambda *_args: ("/remote/system.img", None),
        ))

    def test_exception_after_lock_releases_device_locks(self):
        async def exploding_routes(_devices):
            raise RuntimeError("usbip auto-bind policy boom")

        with contextlib.ExitStack() as stack:
            self._patch_common(stack, usbip_routes=exploding_routes)
            response = self._post_burn()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertEqual(self.locks.released, [("codex@127.0.0.1", ["D1"])])

    def test_api_error_before_lock_returns_502_envelope(self):
        def failing_partition(_ssh, _devices):
            raise ApiError.upstream_failure(
                "无法在测试主机上探测 ADB/Fastboot（设备协议识别失败）",
                details={"adb_exit_code": 1, "fastboot_exit_code": 0},
            )

        with contextlib.ExitStack() as stack:
            self._patch_common(stack, partition=failing_partition)
            response = self._post_burn()

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["code"], "UPSTREAM_FAILURE")
        # 探测失败发生在锁获取之前：不得占用锁，也不得误释放。
        self.assertEqual(self.locks.locked, [])
        self.assertEqual(self.locks.released, [])

    def test_remote_fastboot_hang_returns_timeout_failure_not_forever(self):
        async def timed_out_run_stream(_ssh, _cmd, _cb, timeout=300, **_kwargs):
            return CommandResult(
                stdout="",
                stderr=f"SSH command timed out after {timeout} seconds",
                code=-1,
                timed_out=True,
            )

        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch.object(
                gsi_sn_burn, "prepare_gsi_command",
                new=lambda **_kwargs: "/remote/run.sh --device D1",
            ))
            stack.enter_context(patch.object(
                gsi_sn_burn, "ssh_executor",
                new=SimpleNamespace(run_stream=timed_out_run_stream),
            ))
            response = self._post_burn()

        self.assertEqual(response.status_code, 504)
        body = response.json()
        self.assertIn("部分设备烧写失败", body["error"])
        self.assertIn("timed out", body["results"][0]["error"])
        self.assertFalse(body["results"][0]["success"])
        # 单设备失败后锁仍按正常路径释放。
        self.assertEqual(self.locks.released, [("codex@127.0.0.1", ["D1"])])

    def test_remote_fastboot_failure_returns_502_envelope(self):
        async def failed_run_stream(*_args, **_kwargs):
            return CommandResult(stdout='', stderr='remote fastboot failed', code=1)

        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch.object(gsi_sn_burn, 'prepare_gsi_command', return_value='/remote/run.sh'))
            stack.enter_context(patch.object(gsi_sn_burn, 'ssh_executor',
                                            new=SimpleNamespace(run_stream=failed_run_stream)))
            response = self._post_burn()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['code'], 'UPSTREAM_FAILURE')
        self.assertIn('部分设备烧写失败', response.json()['error'])

    def test_shell_metacharacter_device_serial_rejected_before_remote_work(self):
        """设备序列号先过 sanitize_device_ids：注入串直接 400，不触远端。"""
        with contextlib.ExitStack() as stack:
            partition = stack.enter_context(patch.object(
                gsi_sn_burn, "_partition_devices_by_flash_state",
                return_value=(["D1"], []),
            ))
            response = self.client.post(
                "/api/burn/gsi",
                json={
                    "devices": ["D1;reboot"],
                    "script_path": "/tmp/run_GSI_Burn.sh",
                    "system_img": "/tmp/system.img",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "MALFORMED_REQUEST")
        self.assertIn("Invalid device serial", response.json()["error"])
        partition.assert_not_called()
        self.assertEqual(self.locks.locked, [])

    def test_devices_are_deduplicated_before_partition(self):
        """重复/空设备项归一去重后才进入远端探测。"""
        captured: list[list[str]] = []

        def recording_partition(_ssh, devices):
            captured.append(list(devices))
            return [], list(devices)

        with contextlib.ExitStack() as stack:
            self._patch_common(stack, partition=recording_partition)
            response = self.client.post(
                "/api/burn/gsi",
                json={
                    "devices": ["D1", "D1", "", "D2"],
                    "script_path": "/tmp/run_GSI_Burn.sh",
                    "system_img": "/tmp/system.img",
                },
            )
        self.assertEqual(captured, [["D1", "D2"]])
        # 探测后无可烧写设备：409 冲突信封（不再是无状态码的 500）。
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "STATE_CONFLICT")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
