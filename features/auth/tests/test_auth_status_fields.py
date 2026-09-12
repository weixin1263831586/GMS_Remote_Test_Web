"""auth/status 字段语义测试（从 test_auth_api.py 拆出保持行数预算）。

status 需要直接暴露 principal_type /
credential_mode / needs_authentication，让 gms_rt_auth_status 一次看清
「是 agent token 还是人工会话」「还要不要出示凭据」。
"""

from features.auth.tests.test_auth_api import AuthApiTests


class AuthStatusFieldTests(AuthApiTests):
    def test_auth_status_reports_principal_type_and_credential_mode(self):
        """status 需要直接暴露 principal_type/credential_mode。"""
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "strongpass1"},
        )
        status = self.client.get("/api/auth/status").json()
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["principal_type"], "user")
        self.assertEqual(status["credential_mode"], "session_cookie")
        # authenticated=true 时不应再要求出示凭据；needs_authentication
        # 是 auth_required（部署策略）与登录态的派生，避免两字段同 true 误读。
        self.assertFalse(status["needs_authentication"])


if __name__ == "__main__":
    import unittest

    unittest.main()
