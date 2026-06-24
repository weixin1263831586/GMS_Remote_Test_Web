import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory


class GerritConfigTests(unittest.TestCase):
    def test_profile_update_preserves_unrelated_profiles(self):
        from features.gerrit.config import (
            add_gerrit_personal_profile,
            normalize_gerrit_dashboard_config,
        )

        current = normalize_gerrit_dashboard_config(
            {
                "dashboard_profiles": [
                    {"id": "open", "name": "Open", "query": "status:open"},
                    {"id": "merged", "name": "Merged", "query": "status:merged"},
                ],
                "department_profiles": [
                    {"id": "platform", "name": "Platform", "owners": []},
                ],
                "personal_profiles": [],
            }
        )

        updated = add_gerrit_personal_profile(
            current,
            "Alice",
            "alice@example.com",
            department_id="platform",
        )

        self.assertEqual(
            [profile["id"] for profile in updated["dashboard_profiles"]],
            ["open", "merged"],
        )
        self.assertEqual(updated["department_profiles"][0]["id"], "platform")
        self.assertIn(
            "alice@example.com",
            updated["department_profiles"][0]["owners"],
        )

    def test_redmine_user_sync_builds_department_and_personal_profiles(self):
        from features.gerrit.config import (
            normalize_gerrit_dashboard_config,
            sync_gerrit_members_from_redmine_users,
        )

        current = normalize_gerrit_dashboard_config(
            {
                "department_profiles": [],
                "personal_profiles": [],
            }
        )
        updated = sync_gerrit_members_from_redmine_users(
            current,
            [
                {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "department_id": "platform",
                    "department": "Platform",
                }
            ],
        )

        department = next(
            profile
            for profile in updated["department_profiles"]
            if profile["id"] == "platform"
        )
        personal = next(
            profile
            for profile in updated["personal_profiles"]
            if profile["owner"] == "alice@example.com"
        )
        self.assertEqual(department["owners"], ["alice@example.com"])
        self.assertEqual(personal["department_id"], "platform")

    def test_feature_gerrit_config_save_preserves_other_runtime_sections(self):
        from features.gerrit.settings import GerritConfig

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foundation").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({"gerrit_dashboard": {"base_url": "https://old.example.com"}}),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text(
                json.dumps({"redmine_auth": {"username": "u"}, "sidebar_order": ["test"]}),
                encoding="utf-8",
            )

            manager = GerritConfig(project_root=root)

            self.assertTrue(manager.save_gerrit_dashboard_config({
                "base_url": "https://10.10.10.29/",
                "department_profiles": [{"id": "sys", "name": "系统部", "owners": ["dev@example.com"]}],
            }))

            runtime = json.loads((configs / "config_runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["redmine_auth"]["username"], "u")
            self.assertEqual(runtime["sidebar_order"], ["test"])
            self.assertEqual(runtime["gerrit_dashboard"]["base_url"], "https://10.10.10.29")


if __name__ == "__main__":
    unittest.main()
