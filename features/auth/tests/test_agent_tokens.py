"""Agent Service Token / Approval Token / Enrollment unit tests.

Covers the 2026-09-08 audit changes:
- agent token create/list/revoke + principal resolution with scopes
- one-shot approval token binding (tool+device+command hash, TTL, single use)
- enrollment code redeem (one-shot, TTL) and worker/device ACL checks
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.auth.service import (
    AGENT_ROLE,
    APPROVAL_TOKEN_TTL_SECONDS,
    AuthService,
    CurrentUser,
)


class AgentTokenServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = AuthService(db_path=Path(self._tmp.name) / "auth.sqlite3")
        self.service.initialize()
        # Token ownership JOINs platform_users, so the owner must be a real row.
        self.admin = self.service.create_user(
            "admin", "password123", role="admin"
        )
        record = self.service.create_agent_token(
            name="codex-build01",
            owner=self.admin,
            scopes=["devices.read", "devices.read", "tests.execute"],
            allowed_workers=["w1", "w2"],
        )
        token = record["token"]
        self.assertTrue(token)
        # Only the SHA-256 hash is persisted; the raw token never lands in DB.
        import sqlite3

        with sqlite3.connect(self.service.db_path) as conn:
            rows = conn.execute(
                "SELECT token_hash FROM platform_agent_tokens"
            ).fetchall()
        self.assertNotIn(token, {row[0] for row in rows})
        self.assertNotIn(token, str(self.service.list_agent_tokens()))
        principal, agent_record = self.service.get_agent_token_principal(token)
        self.assertIsNotNone(principal)
        self.assertEqual(principal.role, AGENT_ROLE)
        self.assertTrue(principal.has_permission("tests.execute"))
        self.assertFalse(principal.has_permission("firmware.burn"))
        self.assertFalse(principal.has_permission("*"))
        self.assertTrue(
            self.service.agent_acl_allows(agent_record, "workers", "w1")
        )
        self.assertFalse(
            self.service.agent_acl_allows(agent_record, "workers", "w3")
        )
        self.assertTrue(
            self.service.agent_acl_allows(agent_record, "devices", "any")
        )

    def test_unknown_and_revoked_tokens_fail_closed(self):
        record = self.service.create_agent_token(
            name="kimi-build02", owner=self.admin, scopes=["devices.read"]
        )
        self.assertIsNone(self.service.get_agent_token_principal("bogus")[0])
        self.assertTrue(self.service.revoke_agent_token(record["id"]))
        principal, _ = self.service.get_agent_token_principal(record["token"])
        self.assertIsNone(principal)
        # Revoked token again: still None, and a second revoke reports False.
        principal2, _ = self.service.get_agent_token_principal(record["token"])
        self.assertIsNone(principal2)
        self.assertFalse(self.service.revoke_agent_token(record["id"]))

    def test_scopes_are_whitelisted(self):
        with self.assertRaises(ValueError):
            self.service.create_agent_token(
                name="bad", owner=self.admin, scopes=["firmware.burn"]
            )

    def test_expired_token_fails(self):
        record = self.service.create_agent_token(
            name="shortlived", owner=self.admin, scopes=["devices.read"],
            expires_days=1,
        )
        # Force expiry by rewriting the row.
        import sqlite3

        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute(
                "UPDATE platform_agent_tokens SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", record["id"]),
            )
        principal, _ = self.service.get_agent_token_principal(record["token"])
        self.assertIsNone(principal)


class ApprovalTokenServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = AuthService(db_path=Path(self._tmp.name) / "auth.sqlite3")
        self.service.initialize()
        self.user = CurrentUser("hcq@1.2.3.4", "hcq@1.2.3.4", "user")

    def test_single_use_correct_binding(self):
        record = self.service.create_approval_token(
            user=self.user,
            tool="gms_rt_shell_exec",
            device="RK3572",
            command="settings put global wifi_on 1",
        )
        token = record["token"]
        ok = self.service.consume_approval_token(
            token,
            tool="gms_rt_shell_exec",
            device="RK3572",
            command="settings put global wifi_on 1",
        )
        self.assertTrue(ok)
        # Single use: second consume fails even with the same binding.
        ok2 = self.service.consume_approval_token(
            token,
            tool="gms_rt_shell_exec",
            device="RK3572",
            command="settings put global wifi_on 1",
        )
        self.assertFalse(ok2)

    def test_command_swap_detected(self):
        record = self.service.create_approval_token(
            user=self.user,
            tool="gms_rt_shell_exec",
            device="RK3572",
            command="settings put global wifi_on 1",
        )
        ok = self.service.consume_approval_token(
            record["token"],
            tool="gms_rt_shell_exec",
            device="RK3572",
            command="rm -rf /data",
        )
        self.assertFalse(ok)

    def test_device_and_tool_mismatch_detected(self):
        record = self.service.create_approval_token(
            user=self.user,
            tool="gms_rt_shell_exec",
            device="RK3572",
            command="reboot",
        )
        ok = self.service.consume_approval_token(
            record["token"], tool="gms_rt_shell_exec", device="OTHER", command="reboot"
        )
        self.assertFalse(ok)
        ok2 = self.service.consume_approval_token(
            record["token"], tool="gms_rt_burn_firmware", device="RK3572", command="reboot"
        )
        self.assertFalse(ok2)

    def test_ttl_enforced(self):
        self.assertLessEqual(APPROVAL_TOKEN_TTL_SECONDS, 600)
        record = self.service.create_approval_token(
            user=self.user, tool="gms_rt_shell_exec", device="D1", command="ls",
            ttl_seconds=30,
        )
        # Force expiry.
        import sqlite3

        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute(
                "UPDATE platform_approval_tokens SET expires_at = ?",
                ("2000-01-01T00:00:00+00:00",),
            )
        ok = self.service.consume_approval_token(
            record["token"], tool="gms_rt_shell_exec", device="D1", command="ls"
        )
        self.assertFalse(ok)


class EnrollmentServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = AuthService(db_path=Path(self._tmp.name) / "auth.sqlite3")
        self.service.initialize()
        self.admin = self.service.create_user(
            "admin", "password123", role="admin"
        )

    def test_enroll_redeems_once_and_carries_scopes(self):
        enrollment = self.service.create_agent_enrollment(
            name="codex-build01",
            creator=self.admin,
            scopes=["devices.read", "tests.execute"],
            allowed_workers="w1",
        )
        code = enrollment["code"]
        self.assertLessEqual(enrollment["ttl_minutes"], 5)
        record = self.service.redeem_agent_enrollment(code)
        self.assertIsNotNone(record)
        self.assertIn("tests.execute", record["scopes"])
        self.assertEqual(record["allowed_workers"], "w1")
        principal, agent_record = self.service.get_agent_token_principal(
            record["token"]
        )
        self.assertIsNotNone(principal)
        self.assertTrue(
            self.service.agent_acl_allows(agent_record, "workers", "w1")
        )
        # One shot: the same code can never redeem again.
        self.assertIsNone(self.service.redeem_agent_enrollment(code))
        self.assertIsNone(self.service.redeem_agent_enrollment(code.lower()))

    def test_bogus_code_rejected(self):
        self.assertIsNone(self.service.redeem_agent_enrollment("AAAA-BBBB-CCCC"))
        self.assertIsNone(self.service.redeem_agent_enrollment(""))


if __name__ == "__main__":
    unittest.main()
