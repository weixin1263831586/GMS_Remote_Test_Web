"""R05 / R09 acceptance tests (2026-09-08 audit round 2).

R05 — Agent 配对码兑换必须在开启全局鉴权的完整应用上匿名可用：
以前的测试继承根 conftest 的 GMS_AUTH_REQUIRED=false，隔离 router 通过
但部署入口（audit middleware 401）必然失败。这里用与部署一致的
production 环境（鉴权开启）走 create_app() 完整入口。

R09 — 配对码 / Agent Service Token 不得进入安全审计明文：
认证入口的请求与响应正文必须被专用 schema 替换为占位标记，其他业务
接口（对照组）不受影响。
"""

from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service
from features.system import security_audit_logger


class AgentEnrollmentPublicAccessTests(unittest.TestCase):
    """R05: full-app entry point with GMS_AUTH_REQUIRED=true."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.original_audit_path = security_audit_logger.log_path
        self.original_audit_lock_path = security_audit_logger.lock_path
        security_audit_logger.log_path = str(
            Path(self.tmp.name) / "security_audit.json"
        )
        security_audit_logger.lock_path = (
            f"{security_audit_logger.log_path}.lock"
        )
        security_audit_logger._head_hash = None
        self.environment = unittest.mock.patch.dict(
            "os.environ",
            {
                "GMS_AUTH_REQUIRED": "true",
                "GMS_ENV": "production",
                "GMS_SECURE_COOKIES": "true",
                "GMS_SECRET_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
                "GMS_AUDIT_HMAC_KEY": "audit-key-for-r05-r09-tests-000000001",
                "GMS_METRICS_TOKEN": "metrics-token-for-r05-r09-tests-000001",
                "GMS_AUTOMATION_WEBHOOK_TOKEN": "webhook-token-r05-r09-tests-0001",
                "GMS_AUTOMATION_OWNER_ID": "service-automation",
                "GMS_BOOTSTRAP_TOKEN": "bootstrap-token-for-r05-r09-tests-001",
                "GMS_CLUSTER_CONFIG": str(
                    Path(self.tmp.name) / "cluster.json"
                ),
                "GMS_WORKER_TOKENS_FILE": str(
                    Path(self.tmp.name) / "worker_tokens.json"
                ),
                "GMS_ALLOWED_ORIGINS": "https://testserver",
                "TRUSTED_HOSTS": "testserver",
            },
        )
        (Path(self.tmp.name) / "cluster.json").write_text("{}", encoding="utf-8")
        (Path(self.tmp.name) / "worker_tokens.json").write_text(
            json.dumps(
                {
                    "worker_tokens": {
                        "r05-r09-test-worker": "worker-token-for-r05-r09-tests-0001"
                    }
                }
            ),
            encoding="utf-8",
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        auth_service.initialize()
        self.client = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(self.client.close)
        if auth_service.setup_required():
            resp = self.client.post(
                "/api/auth/setup",
                headers={"X-GMS-Bootstrap-Token": "bootstrap-token-for-r05-r09-tests-001"},
                json={"username": "admin", "password": "password123"},
            )
            assert resp.status_code == 200, resp.text
        # Elevated admin session mints the enrollment code.
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 200, resp.text
        resp = self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 200, resp.text

    def tearDown(self):
        self.client.close()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized
        security_audit_logger.log_path = self.original_audit_path
        security_audit_logger.lock_path = self.original_audit_lock_path
        security_audit_logger._head_hash = None

    def _mint_code(self, name: str = "codex-build01") -> str:
        resp = self.client.post(
            "/api/auth/agent-enrollment-codes",
            json={
                "name": name,
                "scopes": ["system.read", "tests.execute"],
                "allowed_workers": "*",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["enrollment"]["code"]

    def test_anonymous_redemption_succeeds_on_auth_enabled_full_app(self):
        code = self._mint_code()
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        resp = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["success"])
        self.assertIn("tests.execute", resp.json()["token"]["scopes"])
        # The enrolled token authenticates without any cookie.
        token = resp.json()["token"]["token"]
        resp = bare.get(
            "/api/auth/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertTrue(resp.json()["authenticated"])

    def test_redemption_replay_is_rejected(self):
        code = self._mint_code()
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        first = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(first.status_code, 200, first.text)
        replay = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(replay.status_code, 403)

    def test_invalid_code_fails_without_session(self):
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        resp = bare.post(
            "/api/auth/agent-enroll", json={"code": "not-a-real-code"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_enroll_route_is_only_public_for_exact_post(self):
        # Other methods on the same path stay session-gated.
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        resp = bare.get("/api/auth/agent-enroll")
        self.assertEqual(resp.status_code, 401)
        # Adjacent auth endpoints stay gated as before.
        resp = bare.get("/api/auth/agent-tokens")
        self.assertEqual(resp.status_code, 401)

    def test_openapi_marks_enroll_route_public(self):
        schema = self.client.get("/openapi.json").json()
        enroll = schema["paths"]["/api/auth/agent-enroll"]["post"]
        self.assertEqual(enroll.get("security"), [])


class EnrollmentAuditRedactionTests(AgentEnrollmentPublicAccessTests):
    """R09: 配对码/Token 不进入审计明文（完整应用 + 真实审计链）。"""

    def _read_audit_text(self) -> str:
        try:
            return Path(security_audit_logger.log_path).read_text(
                encoding="utf-8"
            )
        except FileNotFoundError:
            return ""

    def test_code_and_token_never_reach_audit_log(self):
        code = self._mint_code()
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        resp = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(resp.status_code, 200, resp.text)
        raw_token = resp.json()["token"]["token"]

        audit_text = self._read_audit_text()
        self.assertIn("/api/auth/agent-enroll", audit_text), (
            "audit must keep the redemption event itself"
        )
        self.assertNotIn(code, audit_text, "配对码泄漏进审计日志")
        self.assertNotIn(raw_token, audit_text, "Agent token 泄漏进审计日志")
        # 占位标记存在，证明专用脱敏 schema 生效（而非整条事件缺失）。
        self.assertIn("认证凭据正文不记录", audit_text)

        # Mint side: the enrollment-code response carries `enrollment.code`;
        # the audit copy must be redacted there too.
        self.assertNotIn(code, audit_text)

    def test_business_code_fields_are_not_blanket_redacted(self):
        # 对照组：R09 的路径级脱敏只精确匹配认证入口——登录接口的正文
        # 仍照常摘要。模块级单元断言在
        # features/system/tests/test_audit_credential_redaction.py
        # （本测试不跨 feature 依赖 system 内部子模块，架构门禁约束）。
        resp = self.client.post(
            "/api/auth/login", json={"username": "admin", "password": "x"}
        )
        self.assertIn(resp.status_code, (401, 403))
        audit_text = self._read_audit_text()
        # 登录失败事件被审计（正文为普通业务字段，不做凭据占位）。
        self.assertIn("/api/auth/login", audit_text)
        self.assertNotIn("认证凭据正文不记录", audit_text.split("/api/auth/login", 1)[1])


if __name__ == "__main__":
    unittest.main()
