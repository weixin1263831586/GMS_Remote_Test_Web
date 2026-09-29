"""`/api/ssh/sshd` 与 `/api/ssh/route` 的人类主体边界测试。

这两个端点会以服务端存储的凭据对调用者指定的 ``user@ip`` 发起 SSH
连接（sshd 还会读取目标主机的密码做连接探测），与 ``/api/ssh/ping``、
``/api/vpn/*`` 同属人类运维面。它们曾缺失 ``_HUMAN_ONLY``，导致全局
认证中间件拦住了匿名请求、却放行了 Agent Service Token——非人类主体
可借平台凭据对任意主机发起 SSH 连接。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.system import integrations


_SSH_HELPER_PATHS = ("/api/ssh/sshd", "/api/ssh/route")


class _FakeConfigManager:
    def load_config(self):
        return {}


class SshHelperAccessTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"GMS_AUTH_REQUIRED": "1"})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            mode = request.headers.get("X-Test-Auth", "")
            if mode == "human":
                request.state.current_user = CurrentUser(
                    id="id-alice", username="alice", role="user",
                )
            elif mode == "agent":
                request.state.current_user = CurrentUser(
                    id="agent:1", username="agent:1", role="agent_service",
                )
                request.state.auth_method = "agent_token"
            return await call_next(request)

        app.include_router(integrations.router)
        self.client = TestClient(app)

    def test_anonymous_gets_401(self):
        for path in _SSH_HELPER_PATHS:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

    def test_agent_token_gets_403(self):
        for path in _SSH_HELPER_PATHS:
            with self.subTest(path=path):
                response = self.client.get(
                    path, headers={"X-Test-Auth": "agent"},
                )
                self.assertEqual(response.status_code, 403)

    def test_human_session_passes_the_gate(self):
        """人类会话不被新边界拦截（处理器业务结果可以是 400/404 等）。"""
        with (
            patch.object(integrations, "config_manager", _FakeConfigManager()),
            patch.object(
                integrations, "resolve_tailscale_device_host",
                new=lambda *_args: (None, False),
            ),
            patch.object(
                integrations, "get_client_display_id_from_request",
                new=lambda *_args: "",
            ),
        ):
            for path in _SSH_HELPER_PATHS:
                with self.subTest(path=path):
                    response = self.client.get(
                        path, headers={"X-Test-Auth": "human"},
                    )
                    self.assertNotIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
