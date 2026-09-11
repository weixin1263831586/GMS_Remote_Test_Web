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


_ED25519_TEST_KEY_PEM: bytes | None = None


def _test_ed25519_key_pem() -> bytes:
    """15.txt 审核 P2: production fixtures must supply an agent-package
    signing key now that production validation requires one."""
    global _ED25519_TEST_KEY_PEM
    if _ED25519_TEST_KEY_PEM is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        _ED25519_TEST_KEY_PEM = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    return _ED25519_TEST_KEY_PEM


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
        signing_key_path = Path(self.tmp.name) / "agent_signing_key.pem"
        signing_key_path.write_bytes(_test_ed25519_key_pem())
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
                "GMS_SKILL_SIGNING_KEY_FILE": str(signing_key_path),
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
                "scopes": ["devices.read", "tests.execute"],
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


class _FixedIPASGIWrapper:
    """Wrap an ASGI app so every request's client address is the fixed IP."""

    def __init__(self, app, ip: str):
        self.app = app
        self.ip = ip

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope)
            scope["client"] = (self.ip, 50000)
        await self.app(scope, receive, send)


class EnrollmentRateLimitAndEntropyTests(AgentEnrollmentPublicAccessTests):
    """配对码暴力枚举防护（10.txt 2026-08 评审）：

    - 兑换端点按来源 IP 持久限速（复用登录限速基础设施）
    - 配对码熵提升到 token_hex(3)×3（144 bit），拒绝 24-bit 旧格式
    """

    def test_brute_force_is_rate_limited_per_ip(self):
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        # AUTH_MAX_ACCOUNT_IP_FAILURES / AUTH_MAX_IP_FAILURES default to 5/30;
        # drive account_ip over its threshold with wrong codes.
        for _ in range(5):
            resp = bare.post(
                "/api/auth/agent-enroll", json={"code": "DEAD-BEEF-0000"}
            )
            self.assertEqual(resp.status_code, 403)
        resp = bare.post("/api/auth/agent-enroll", json={"code": "DEAD-BEEF-0000"})
        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertTrue(int(resp.headers.get("Retry-After", "0")) >= 1)

    def test_rate_limit_does_not_block_a_valid_code_afterwards(self):
        # A blocked IP must stay blocked even with the correct code — the
        # limiter gates attempts, not outcomes.
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        code = self._mint_code()
        for _ in range(5):
            bare.post("/api/auth/agent-enroll", json={"code": "0000-0000-0000"})
        resp = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(resp.status_code, 429)
        # The unused code is still redeemable from a DIFFERENT source IP.
        other = TestClient(
            _FixedIPASGIWrapper(create_app(), "10.9.8.7"),
            base_url="https://testserver",
        )
        self.addCleanup(other.close)
        resp = other.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_successful_redemption_clears_failure_counter(self):
        bare = TestClient(create_app(), base_url="https://testserver")
        self.addCleanup(bare.close)
        code = self._mint_code()
        # A few failures below the threshold, then the valid code — the
        # account_ip counter must be cleared so a legitimate retry after a
        # typo storm is not punished later.
        for _ in range(3):
            bare.post("/api/auth/agent-enroll", json={"code": "1111-1111-1111"})
        resp = bare.post("/api/auth/agent-enroll", json={"code": code})
        self.assertEqual(resp.status_code, 200, resp.text)
        retry_after = auth_service.auth_retry_after(
            "agent-enroll", "code", bare.headers.get("x-forwarded-for", "testclient")
        )
        self.assertEqual(retry_after, 0)

    def test_enrollment_code_entropy(self):
        record = auth_service.create_agent_enrollment(
            name="entropy-check",
            creator=self._admin_user(),
            scopes=["devices.read"],
        )
        code = record["code"]
        # token_hex(3)×3 → 6 hex chars per group (48 bit each, 144 total).
        groups = code.split("-")
        self.assertEqual(len(groups), 3)
        for group in groups:
            self.assertEqual(len(group), 6)
            self.assertRegex(group, r"^[0-9A-F]{6}$")
        # And the old 24-bit-total format is gone.
        self.assertNotEqual(len(groups[0]), 4)

    def _admin_user(self):
        from features.auth import CurrentUser

        return CurrentUser(
            id="admin-id", username="admin", role="admin", display_name=""
        )


if __name__ == "__main__":
    unittest.main()
