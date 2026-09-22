"""认证预检测试：Daily Brief 语义必须 fail-closed。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.kkagent import auth_preflight as mod


class PreflightGmsAuthTests(unittest.TestCase):
    """无 profile / selfcheck 不可用 / 超时 / 明确未认证 → 全部拦截。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write_selfcheck(self, payload: str) -> Path:
        script = self.dir / "selfcheck.sh"
        script.write_text(f"#!/bin/sh\necho '{payload}'\n", encoding="utf-8")
        return script

    def _payload(self, authenticated: bool) -> str:
        return json.dumps({
            "ok": authenticated,
            "exit_code": 0 if authenticated else 3,
            "data": {
                "profile": "kkagent-host-x",
                "credential": {"token_file": "/tmp/tok"},
                "auth": {"ok": True,
                         "status": {"authenticated": authenticated}},
            },
        })

    def _preflight(self, script: Path | None, **kw):
        import asyncio

        env = {"GMS_RT_PROFILE": "kkagent-host-x"}
        with patch.object(mod, "GMS_SELFCHECK_SCRIPT",
                          str(script or self.dir / "nope.sh")), \
                patch("shutil.which", return_value=None):
            return asyncio.run(mod.preflight_gms_auth(env, **kw))

    def test_no_profile_is_blocked_fail_closed(self):
        """Daily Brief 强制 Redmine 只读取证：无 profile 即 analysis_unavailable。"""
        import asyncio

        ok, reason = asyncio.run(mod.preflight_gms_auth({}))
        self.assertFalse(ok)
        self.assertIn("analysis_unavailable", reason)
        self.assertIn("agent_profile", reason)

    def test_revoked_token_blocks_with_reenroll_hint(self):
        ok, reason = self._preflight(self._write_selfcheck(self._payload(False)))
        self.assertFalse(ok)
        self.assertIn("kkagent-host-x", reason)
        self.assertIn("/tmp/tok", reason)
        self.assertIn("gms-rt-agent-enroll", reason)

    def test_authenticated_token_passes(self):
        ok, reason = self._preflight(self._write_selfcheck(self._payload(True)))
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_selfcheck_missing_or_garbage_fails_closed(self):
        # 脚本不存在且 PATH 上没有：不能放行"凭上下文猜"的晨报。
        ok, reason = self._preflight(None)
        self.assertFalse(ok)
        self.assertIn("gms-rt-agent-enroll", reason)
        # 输出非法 JSON：同样拦截。
        ok, reason = self._preflight(self._write_selfcheck("not json"))
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_selfcheck_timeout_fails_closed(self):
        script = self.dir / "slow.sh"
        script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        ok, reason = self._preflight(script, timeout_seconds=0.2)
        self.assertFalse(ok)
        self.assertIn("analysis_unavailable", reason)

    def test_parse_payload_flat_authenticated_fallback(self):
        payload = json.dumps({"data": {"authenticated": True}})
        ok, reason = mod.parse_selfcheck_payload(payload.encode(), {"GMS_RT_PROFILE": "p"})
        self.assertTrue(ok)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
