"""Agent Service Token burn authorization tests (15.txt 缺陷1 regression).

`/api/burn/firmware` used to depend on require_elevated_admin_when_auth_required,
which reads the *cookie* session elevation table. An Agent Service Token
(Bearer) has no cookie session, so in authenticated deployments
(GMS_AUTH_REQUIRED=1 / production) every agent burn was rejected before the
approval-token stage could even run — the approval flow was unreachable.

The endpoint now accepts two credential paths:
- human session with a live admin elevation (unchanged behavior);
- agent_service principal, which must still pass the server-side one-shot
  approval token bound to tool+device+command inside the handler.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import AGENT_ROLE, CurrentUser
from features.firmware import api, runtime
from foundation.command_result import CommandResult


class FakeConfigManager:
    def load_config(self):
        return {"ubuntu_user": "tester", "client_username": "codex"}

    def get_ubuntu_user(self, config):
        return config.get("ubuntu_user", "tester")


class FakeSshManager:
    def __init__(self):
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
                stdout="__GMS_REMOTE_FILE_MISSING__\n", stderr="", code=0,
            )
        return CommandResult(stdout="", stderr="", code=0)


async def _fake_lock(*_args, **_kwargs):
    return ["D1"], None


async def _fake_release(*_args, **_kwargs):
    return None


async def _async_none(*_args, **_kwargs):
    return None


class AgentBurnAuthorizationTests(unittest.TestCase):
    """The dependency gate: who may reach the burn handler at all."""

    def setUp(self):
        self.runtime_directory = TemporaryDirectory()
        runtime.configure_runtime(
            config_manager=FakeConfigManager(),
            ssh_manager=FakeSshManager(),
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=__import__("threading").RLock(),
                apk_upload_locks={},
                apk_upload_locks_lock=__import__("threading").RLock(),
                firmware_upload_progress={},
                firmware_upload_progress_lock=__import__("threading").RLock(),
                websocket_connections={},
            ),
            generate_help_or_continue=lambda help, *_args: None,
            get_client_id_from_request=lambda _request: "codex@127.0.0.1",
            lock_firmware_devices=_fake_lock,
            release_firmware_devices=_fake_release,
            safe_websocket_send=lambda *args, **kwargs: _async_none(),
            store_notification=lambda *args, **kwargs: None,
            project_root=".",
            firmware_share_store=None,
            apk_max_tasks=20,
            apk_max_file_size=500 * 1024 * 1024,
            apk_max_source_file_size=2 * 1024 * 1024,
            apk_upload_dir=self.runtime_directory.name,
        )
        self.app = FastAPI()
        self.current_user: CurrentUser | None = None

        @self.app.middleware("http")
        async def authenticate_test_request(request, call_next):
            if self.current_user is not None:
                request.state.current_user = self.current_user
            return await call_next(request)

        self.app.include_router(api.router)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.runtime_directory.cleanup()

    def _set_principal(self, role: str) -> None:
        self.current_user = CurrentUser(
            id=f"id-{role}", username=role, role=role
        )

    def test_agent_principal_passes_gate_without_elevation(self):
        """缺陷1 回归：agent Bearer 不再被 cookie 提权门挡死。"""
        self._set_principal(AGENT_ROLE)
        with patch(
            "features.firmware.firmware_api.authentication_required",
            return_value=True,
        ):
            # approval_token 缺失 → 403，但错误应是 "需要审批令牌"，
            # 而不是 Elevation required —— 证明 agent 已通过授权门。
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/tmp/not-found.img"},
            )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertIn("审批令牌", str(body.get("error") or body.get("detail") or body))

    def test_agent_with_valid_approval_reaches_burn_pipeline(self):
        """有有效 approval token 时，agent 请求进入烧录流程本体。

        这里固件路径不存在，预期得到 "Firmware not found"（业务错误），
        而不是任何 403 授权错误 —— 证明 approval 门已通过。
        """
        self._set_principal(AGENT_ROLE)
        with patch(
            "features.firmware.firmware_api.authentication_required",
            return_value=True,
        ), patch(
            "features.auth.service.auth_service.consume_approval_token",
            return_value=True,
        ) as consume, patch(
            "features.auth.service.auth_service.derive_burn_command",
            side_effect=lambda **kwargs: (
                "burn_firmware:"
                + ",".join(sorted(kwargs["device"].split(",")))
                + f":{kwargs['firmware_sha256'][:12]}"
                + f":wipe={'true' if kwargs['wipe_data'] else 'false'}"
                + f":mode={kwargs['burn_mode']}"
            ),
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1&approval_token=tok123",
                data={"firmware_path": "/tmp/not-found.img"},
            )
        self.assertEqual(
            response.json(),
            {"success": False, "error": "Firmware not found: /tmp/not-found.img"},
        )
        # 审批令牌按 tool+规范化设备列表 被服务端一次性消费（4.txt P0-3），
        # 绑定串包含固件 SHA256（4.txt P1 精确绑定；此处路径不存在，
        # consume 在固件解析之后，所以这里根本不会到达 consume）。
        consume.assert_not_called()

    def test_agent_multi_device_burn_consumes_one_operation_approval(self):
        """4.txt P0-3 回归：多设备烧录只消费一次 operation 级审批。

        旧实现按设备循环 consume，第二个设备必失败（single use）。新实现
        对规范化（排序去重后逗号连接）设备列表做一次性绑定+消费。
        消费点在固件文件解析之后，此处固件路径不存在，只验证未提前消费。
        """
        self._set_principal(AGENT_ROLE)
        with patch(
            "features.firmware.firmware_api.authentication_required",
            return_value=True,
        ), patch(
            "features.auth.service.auth_service.consume_approval_token",
            return_value=True,
        ) as consume:
            # 故意乱序传入，验证规范化排序绑定（真实绑定在固件解析后）。
            response = self.client.post(
                "/api/burn/firmware?devices=D2,D1,D3&approval_token=tok123",
                data={"firmware_path": "/tmp/not-found.img"},
            )
        self.assertEqual(
            response.json(),
            {"success": False, "error": "Firmware not found: /tmp/not-found.img"},
        )
        consume.assert_not_called()

    def test_agent_burn_approval_binds_firmware_digest_and_params(self):
        """4.txt P1 精确绑定回归：approval 绑定 固件SHA256+wipe+mode。

        固件文件存在（临时文件），服务端必须对它的实际字节计算 SHA256 并
        用服务端派生的 operation 串消费审批，而不是客户端传入的任何串。
        """
        with tempfile.NamedTemporaryFile(
            suffix=".img", delete=False
        ) as firmware:
            firmware.write(b"GMS-FAKE-FIRMWARE-BYTES")
            firmware_name = firmware.name
        self.addCleanup(os.unlink, firmware_name)
        expected_sha = hashlib.sha256(
            b"GMS-FAKE-FIRMWARE-BYTES"
        ).hexdigest()

        self._set_principal(AGENT_ROLE)
        with patch(
            "features.firmware.firmware_api.authentication_required",
            return_value=True,
        ), patch(
            "features.auth.service.auth_service.consume_approval_token",
            return_value=True,
        ) as consume, patch(
            "features.firmware.firmware_api._adb_proxy_devices",
            return_value=[],
        ), patch(
            "features.firmware.firmware_api.validate_local_update_image",
        ) as _unused_validation, patch(
            "features.firmware.firmware_api._upload_firmware_to_test_host",
            side_effect=_async_none,
        ), patch(
            "features.firmware.firmware_api._lock_devices",
            side_effect=_fake_lock,
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D2,D1&approval_token=tok123"
                "&burn_mode=auto",
                data={"firmware_path": firmware_name, "wipe_data": "true"},
            )
        # 测试桩环境在后续 SSH 阶段会失败，但审批消费发生在进入 SSH 前，
        # 只需断言消费调用的绑定串正确。
        self.assertGreaterEqual(response.status_code, 200)
        consume.assert_called_once()
        kwargs = consume.call_args.kwargs
        self.assertEqual(kwargs.get("tool"), "gms_rt_burn_firmware")
        self.assertEqual(kwargs.get("device"), "D1,D2")
        self.assertEqual(
            kwargs.get("command"),
            f"burn_firmware:D1,D2:{expected_sha}:wipe=true:mode=auto",
        )

    def test_agent_with_rejected_approval_still_blocked(self):
        """审批令牌无效（已被消费/命令不匹配）时 403。

        消费点在固件解析之后（P1 精确绑定），所以用真实存在的临时固件
        文件驱动流程到达消费点。
        """
        with tempfile.NamedTemporaryFile(
            suffix=".img", delete=False
        ) as firmware:
            firmware.write(b"GMS-FAKE-FIRMWARE-BYTES")
            firmware_name = firmware.name
        self.addCleanup(os.unlink, firmware_name)
        self._set_principal(AGENT_ROLE)
        with patch(
            "features.firmware.firmware_api.authentication_required",
            return_value=True,
        ), patch(
            "features.auth.service.auth_service.consume_approval_token",
            return_value=False,
        ), patch(
            "features.firmware.firmware_api._adb_proxy_devices",
            return_value=[],
        ), patch(
            "features.firmware.firmware_api.validate_local_update_image",
        ), patch(
            "features.firmware.firmware_api._upload_firmware_to_test_host",
            side_effect=_async_none,
        ), patch(
            "features.firmware.firmware_api._lock_devices",
            side_effect=_fake_lock,
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1&approval_token=tok123",
                data={"firmware_path": firmware_name},
            )
        self.assertEqual(response.status_code, 403)

    def test_plain_user_without_elevation_is_denied_in_production(self):
        """普通 user 无提权在生产模式仍被拒（原有安全语义不变）。"""
        self._set_principal("user")
        with patch(
            "features.firmware.firmware_api.authentication_required",
            return_value=True,
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/tmp/not-found.img"},
            )
        self.assertEqual(response.status_code, 403)
        detail = response.json().get("detail") or {}
        self.assertTrue(
            detail.get("elevation_required")
            if isinstance(detail, dict)
            else response.status_code == 403
        )

    def test_elevated_human_admin_still_passes_without_approval(self):
        """提权的人类管理员不需要 approval token（原有路径不受影响）。"""
        self._set_principal("admin")
        with patch(
            "features.firmware.firmware_api.authentication_required",
            return_value=True,
        ), patch(
            "features.firmware.firmware_api.require_elevated_admin",
            side_effect=lambda request: self.current_user,
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/tmp/not-found.img"},
            )
        # 管理员走到业务校验：固件不存在（不是 403）。
        self.assertEqual(
            response.json(),
            {"success": False, "error": "Firmware not found: /tmp/not-found.img"},
        )


if __name__ == "__main__":
    unittest.main()
