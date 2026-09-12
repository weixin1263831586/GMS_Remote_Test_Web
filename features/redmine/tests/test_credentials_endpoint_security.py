"""POST /config/credentials 安全边界测试（反馈 2026-09-11 S-1）。

- human 会话可以写入；
- Agent Service Token 必须被拒（403 + 修复指引）；
- 写入成功记入安全审计链（不含凭据内容）；
- GET /config/credentials 保持 agent 可读（pre-flight 用）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser


def _owner_config_factory(root: Path):
    class ConfigFactory:
        def for_owner(self, owner_id: str):
            from features.redmine.config import RedmineConfig

            manager = RedmineConfig(Path.cwd())
            runtime_path = root / owner_id / "config_runtime.json"
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            manager.runtime_config_path = runtime_path
            return manager

    return ConfigFactory()


class CredentialsEndpointSecurityTests(unittest.TestCase):
    def setUp(self):
        self.secret_env = patch.dict(
            "os.environ",
            {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")},
        )
        self.secret_env.start()
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)

        import features.redmine.api as redmine_api

        self.redmine_api = redmine_api
        app = FastAPI()

        @app.middleware("http")
        async def authenticate(request, call_next):
            auth_mode = request.headers.get("x-test-auth", "human")
            owner = request.headers.get("x-test-owner", "owner-a")
            if auth_mode == "agent":
                request.state.current_user = CurrentUser(
                    id="agt_x", username="agent", role="agent_service",
                    extra_permissions=frozenset({"redmine.read"}),
                )
                request.state.auth_method = "agent_token"
                request.state.agent_token_record = {"owner_user_id": owner}
            else:
                request.state.current_user = CurrentUser(
                    id=owner, username=owner, role="user",
                    extra_permissions=frozenset(),
                )
                request.state.auth_method = "session"
            return await call_next(request)

        app.include_router(redmine_api.router)
        from features.redmine import credentials_api

        app.include_router(credentials_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()
        self.secret_env.stop()

    def _patched(self):
        api = self.redmine_api
        return (
            patch.object(api, "config_manager", _owner_config_factory(self.root)),
            patch.object(api, "_clear_stats_caches", lambda: None),
            patch.dict(
                "os.environ",
                {
                    "GMS_AUTH_REQUIRED": "1",
                    "GMS_ENV": "production",
                },
            ),
        )

    def test_agent_token_cannot_write_credentials(self):
        config_patch, cache_patch, _env_patch = self._patched()
        with config_patch, cache_patch, _env_patch:
            response = self.client.post(
                "/api/redmine-agent/config/credentials",
                json={"username": "u", "password": "p"},
                headers={"x-test-auth": "agent", "x-test-owner": "owner-a"},
            )
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertIn("Web UI", body["error"])
        self.assertTrue(body["detail"]["agent_forbidden"])
        # 未落盘任何凭据。
        self.assertFalse((self.root / "owner-a" / "config_runtime.json").exists())

    def test_human_session_can_write_and_audit_logged(self):
        config_patch, cache_patch, _env_patch = self._patched()
        events: list[dict] = []

        class _AuditLogger:
            def log_event(self, event):
                events.append(event)
                return event

        audit_patch = patch.object(
            __import__("foundation.security_audit", fromlist=["security_audit_logger"]),
            "security_audit_logger",
            _AuditLogger(),
        )
        with config_patch, cache_patch, _env_patch, audit_patch:
            response = self.client.post(
                "/api/redmine-agent/config/credentials",
                json={"username": "human-user", "password": "p"},
                headers={"x-test-owner": "owner-a"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["operation"], "save_redmine_credentials")
        self.assertEqual(events[0]["auth_method"], "session")
        # 审计事件绝不包含凭据明文。
        dumped = repr(events[0])
        self.assertNotIn("human-user", dumped)
        self.assertNotIn(":p", dumped)

    def test_get_credentials_status_readable_by_agent(self):
        config_patch, cache_patch, _env_patch = self._patched()
        with config_patch, cache_patch, _env_patch:
            response = self.client.get(
                "/api/redmine-agent/config/credentials",
                headers={"x-test-auth": "agent", "x-test-owner": "owner-a"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("configured", data)
        self.assertFalse(data["configured"])


if __name__ == "__main__":
    unittest.main()
