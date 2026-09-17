import json
import unittest
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from features.devices import screens_api
from features.devices import support as device_support
from features.devices.models import DeviceActionRequest
from foundation.command_result import CommandResult


class FakeConfigManager:
    def load_config(self):
        return {"ubuntu_host": "192.168.0.2", "ubuntu_user": "test user"}

    def get_ubuntu_user(self, _config):
        return "test user"

    def get_ubuntu_host(self, _config):
        return "192.168.0.2"


class FakeSshManager:
    def __init__(self):
        self.commands = []

    @contextmanager
    def optional_connection(self, _config):
        yield object()

    @asynccontextmanager
    async def async_optional_connection(self, config):
        with self.optional_connection(config) as ssh:
            yield ssh

    def execute_command(self, _ssh, command, timeout=None):
        self.commands.append(command)
        if command.startswith("curl "):
            return CommandResult(stdout="200", stderr="", code=0)
        if command == "which scrcpy":
            return CommandResult(stdout="/usr/bin/scrcpy\n", stderr="", code=0)
        if "pgrep -f" in command:
            return CommandResult(stdout="RUNNING\n", stderr="", code=0)
        return CommandResult(stdout="", stderr="", code=0)


class DeviceScreensApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrcpy_requires_explicit_devices(self):
        with (
            patch.object(
                screens_api.runtime,
                "config_manager",
                FakeConfigManager(),
            ),
            patch.object(
                screens_api.runtime,
                "ssh_manager",
                FakeSshManager(),
            ),
        ):
            response = await screens_api.show_device_screens(
                DeviceActionRequest(devices=[]),
                SimpleNamespace(state=SimpleNamespace()),
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("explicit device selection", body["error"])

    async def test_scrcpy_probes_websockify_on_loopback(self):
        ssh_manager = FakeSshManager()
        # 初始扫描：未在投屏（需启动）；启动后轮询：健康（日志 Connected）。
        health_states = iter([(False, None), (True, "4321")])

        with (
            patch.object(screens_api.runtime, "config_manager", FakeConfigManager()),
            patch.object(screens_api.runtime, "ssh_manager", ssh_manager),
            patch.object(
                screens_api.DeviceUtils,
                "check_scrcpy_healthy",
                side_effect=lambda *_args, **_kw: next(health_states),
            ),
            # fencing 身份接缝改为 device_fencing_owner_id（ADR 0010）：
            # 生产代码不再走 runtime.get_client_id_from_request。
            patch.object(
                screens_api,
                "device_fencing_owner_id",
                return_value="user-id",
            ),
            patch.object(
                device_support,
                "acquire_device_operation_claim",
                return_value=(
                    "operation:scrcpy:test",
                    [{
                        "id": "claim-1",
                        "device_key": "ats-worker-controller:ABC-123",
                        "generation": 1,
                        "owner_id": "user-id",
                    }],
                    None,
                ),
            ),
            patch.object(device_support, "release_device_operation_claim"),
            patch.object(device_support, "audit_device_operation"),
            patch.object(screens_api.asyncio, "sleep", return_value=None),
        ):
            response = await screens_api.show_device_screens(
                DeviceActionRequest(devices=["ABC-123"]),
                SimpleNamespace(state=SimpleNamespace()),
            )

        body = json.loads(response.body)
        self.assertTrue(body["success"])
        # websockify 只监听 127.0.0.1（浏览器走同源代理），可用性探测必须
        # 对 loopback 发起；对 ubuntu_host 的外部地址探测会误报不可用。
        curl_check = "http://127.0.0.1:6080/vnc.html?resize=scale --connect-timeout 3"
        self.assertTrue(any(curl_check in cmd for cmd in ssh_manager.commands))
        self.assertEqual(body["vnc_sessions"][0]["message"], "VNC view available")
        # 直连 URL 对浏览器不可达，不再下发。
        self.assertNotIn("url", body["vnc_sessions"][0])
        self.assertTrue(any("scrcpy -s ABC-123" in cmd for cmd in ssh_manager.commands))
        self.assertFalse(any("--no-control" in cmd for cmd in ssh_manager.commands))

    async def test_scrcpy_start_failure_reports_log_reason(self):
        """进程短暂存活但日志无 Connected 时不得误报成功，须带回日志原因。"""
        ssh_manager = FakeSshManager()

        with (
            patch.object(screens_api.runtime, "config_manager", FakeConfigManager()),
            patch.object(screens_api.runtime, "ssh_manager", ssh_manager),
            # 健康检查始终不通过：进程虽在但投屏从未就绪。
            patch.object(
                screens_api.DeviceUtils,
                "check_scrcpy_healthy",
                return_value=(False, None),
            ),
            patch.object(
                screens_api,
                "_scrcpy_log_tail",
                return_value="[server] ERROR: Could not open video stream: Device is offline",
            ),
            # fencing 身份接缝改为 device_fencing_owner_id（ADR 0010）：
            # 生产代码不再走 runtime.get_client_id_from_request。
            patch.object(
                screens_api,
                "device_fencing_owner_id",
                return_value="user-id",
            ),
            patch.object(
                device_support,
                "acquire_device_operation_claim",
                return_value=(
                    "operation:scrcpy:test",
                    [{
                        "id": "claim-1",
                        "device_key": "ats-worker-controller:ABC-123",
                        "generation": 1,
                        "owner_id": "user-id",
                    }],
                    None,
                ),
            ),
            patch.object(device_support, "release_device_operation_claim"),
            patch.object(device_support, "audit_device_operation"),
            # 轮询等待不真 sleep，失败路径 8 次轮询立即走完。
            patch.object(screens_api.asyncio, "sleep", return_value=None),
        ):
            response = await screens_api.show_device_screens(
                DeviceActionRequest(devices=["ABC-123"]),
                SimpleNamespace(state=SimpleNamespace()),
            )

        body = json.loads(response.body)
        self.assertFalse(body["success"])
        self.assertIn("投屏启动失败", body["message"])
        self.assertIn("Device is offline", body["message"])
        self.assertEqual(body["results"][0]["started"], False)
        self.assertIn("Device is offline", body["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
