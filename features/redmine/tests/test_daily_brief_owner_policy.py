"""Daily Brief must never execute under an administrator owner."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.redmine.daily_brief_owner_policy import is_daily_brief_owner_eligible
from features.redmine.daily_brief_run_starter import DailyBriefRunStarterMixin


class DailyBriefOwnerPolicyTests(unittest.TestCase):
    def test_admin_owner_is_rejected(self):
        with patch(
            "features.redmine.daily_brief_owner_policy.auth_service.get_enabled_user",
            return_value=SimpleNamespace(role="admin"),
        ):
            self.assertFalse(is_daily_brief_owner_eligible("gms"))

    def test_ordinary_and_unmanaged_owners_are_eligible(self):
        with patch(
            "features.redmine.daily_brief_owner_policy.auth_service.get_enabled_user",
            side_effect=[SimpleNamespace(role="user"), None],
        ):
            self.assertTrue(is_daily_brief_owner_eligible("ordinary"))
            self.assertTrue(is_daily_brief_owner_eligible("development-owner"))

    def test_starter_rejects_admin_before_writing_a_job(self):
        starter = DailyBriefRunStarterMixin.__new__(DailyBriefRunStarterMixin)
        starter.owner_id = "gms"
        with patch(
            "features.redmine.daily_brief_run_starter.is_daily_brief_owner_eligible",
            return_value=False,
        ):
            result = starter.start_run("manual")
        self.assertEqual(result["code"], "ADMIN_OWNER_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
