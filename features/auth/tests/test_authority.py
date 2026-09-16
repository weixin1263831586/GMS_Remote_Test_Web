from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from features.auth import authority
from features.auth.principal import CurrentUser


class AutomationAuthorityTests(unittest.TestCase):
    def _admin(self) -> CurrentUser:
        return CurrentUser(
            id="owner-1",
            username="owner",
            role="admin",
            resource_owner_id="owner-1",
        )

    def test_default_flash_requires_firmware_capability(self):
        granted = authority.automation_granted_capabilities(
            self._admin(),
            {"test_type": "CTS"},
        )
        self.assertIn("firmware.stage", granted)

    def test_explicit_flash_skip_does_not_request_firmware_capability(self):
        granted = authority.automation_granted_capabilities(
            self._admin(),
            {"test_type": "CTS", "flash": {"mode": "skip"}},
        )
        self.assertNotIn("firmware.stage", granted)

    def test_capability_roundtrip_preserves_owner_and_plan_permissions(self):
        principal = authority.automation_authority(
            "ats_roundtrip",
            "owner-1",
            ["tests.execute", "devices.lease", "firmware.stage"],
        )
        with patch.object(authority, "_signing_key", return_value=b"k" * 32):
            token = authority.mint_capability_token(principal)
            restored = authority.verify_capability_token(token)

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.id, "automation:ats_roundtrip")
        self.assertEqual(restored.resource_owner_id, "owner-1")
        self.assertTrue(restored.has_permission("tests.execute"))
        self.assertTrue(restored.has_permission("devices.lease"))
        self.assertTrue(restored.has_permission("firmware.stage"))
        self.assertTrue(restored.has_permission("jobs.read"))
        self.assertTrue(restored.has_permission("reports.read"))

    def test_capability_fields_roundtrip_identifiers_with_dots(self):
        principal = authority.automation_authority(
            "ats.run.with.dots",
            "owner.name@example.com",
            ["tests.execute"],
        )
        with patch.object(authority, "_signing_key", return_value=b"k" * 32):
            restored = authority.verify_capability_token(
                authority.mint_capability_token(principal)
            )

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.id, "automation:ats.run.with.dots")
        self.assertEqual(restored.resource_owner_id, "owner.name@example.com")
        self.assertTrue(restored.has_permission("tests.execute"))

    def test_tampered_capability_snapshot_fails_closed(self):
        principal = authority.automation_authority(
            "ats_tamper", "owner-1", ["tests.execute"]
        )
        with patch.object(authority, "_signing_key", return_value=b"k" * 32):
            token = authority.mint_capability_token(principal)
            body = token[len(authority.CAPABILITY_TOKEN_PREFIX):]
            fields = body.split(".")
            self.assertEqual(len(fields), 6)
            fields[4] = authority._encode_token_field('["tests.execute","firmware.stage"]')
            tampered = authority.CAPABILITY_TOKEN_PREFIX + ".".join(fields)
            self.assertIsNone(authority.verify_capability_token(tampered))

    def test_expired_token_is_rejected(self):
        principal = authority.automation_authority(
            "ats_expired", "owner-1", ["tests.execute"]
        )
        with (
            patch.object(authority, "_signing_key", return_value=b"k" * 32),
            patch.object(authority.time, "time", return_value=time.time() - 10_000),
        ):
            token = authority.mint_capability_token(principal, ttl_seconds=60)
        with patch.object(authority, "_signing_key", return_value=b"k" * 32):
            self.assertIsNone(authority.verify_capability_token(token))


if __name__ == "__main__":
    unittest.main()
