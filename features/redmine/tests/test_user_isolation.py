from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.config import RedmineConfig
from features.redmine.repository import RedmineAgentDB
from features.redmine.users import load_user_map_payload_for_owner


def _issue(issue_id: int, subject: str) -> dict:
    return {
        "issue_id": issue_id,
        "subject": subject,
        "status_name": "New",
        "priority_name": "Normal",
        "assigned_to_name": "owner",
        "created_on": "2026-06-01T00:00:00",
        "updated_on": "2026-06-01T00:00:00",
    }


class RedmineUserIsolationTests(unittest.TestCase):
    def test_redmine_services_are_isolated_by_owner(self):
        import features.redmine.api as redmine_api

        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            class ConfigFactory:
                def for_owner(self, owner_id: str):
                    manager = RedmineConfig(Path.cwd())
                    runtime_path = root / owner_id / "config_runtime.json"
                    runtime_path.parent.mkdir(parents=True, exist_ok=True)
                    manager.runtime_config_path = runtime_path
                    return manager

            redmine_api._USER_REDMINE_SERVICES.clear()
            import features.redmine.users as redmine_users
            with (
                patch.object(redmine_api, "config_manager", ConfigFactory()),
                patch.object(redmine_api, "owner_db_path", lambda owner: root / owner / "redmine.sqlite3"),
                patch.object(redmine_api, "owner_docs_dir", lambda owner: root / owner / "docs"),
                patch.object(redmine_api, "owner_attachments_dir", lambda owner: root / owner / "attachments"),
                # 避免迁移逻辑把全局 user_map 拷到真实 configs/。
                patch.object(redmine_users, "owner_user_map_path", lambda owner: root / owner / "redmine_user_map.json"),
                patch.object(redmine_api, "owner_user_map_path", lambda owner: root / owner / "redmine_user_map.json"),
            ):
                alice = redmine_api.get_redmine_service_for_owner("alice")
                bob = redmine_api.get_redmine_service_for_owner("bob")

                alice.repository.upsert_issue(_issue(101, "alice issue"))
                bob.repository.upsert_issue(_issue(101, "bob issue"))

                alice.agent.config_manager.save_redmine_credentials("alice-user", "alice-secret")
                bob.agent.config_manager.save_redmine_credentials("bob-user", "bob-secret")

                self.assertEqual(alice.repository.get_issue(101)["subject"], "alice issue")
                self.assertEqual(bob.repository.get_issue(101)["subject"], "bob issue")
                self.assertEqual(alice.agent.config_manager.load_redmine_credentials()["username"], "alice-user")
                self.assertEqual(bob.agent.config_manager.load_redmine_credentials()["username"], "bob-user")
                self.assertTrue((root / "alice" / "redmine.sqlite3").exists())
                self.assertTrue((root / "bob" / "redmine.sqlite3").exists())

    def test_redmine_owner_service_migrates_legacy_data_once(self):
        import features.redmine.api as redmine_api

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_db = root / "legacy" / "redmine.sqlite3"
            legacy_docs = root / "legacy" / "docs"
            legacy_user_map = root / "legacy" / "redmine_user_map.json"
            legacy_runtime = root / "configs" / "config_runtime.json"
            legacy_repo = RedmineAgentDB(db_path=legacy_db, docs_dir=legacy_docs)
            legacy_repo.upsert_issue(_issue(202, "legacy issue"))
            legacy_docs.mkdir(parents=True, exist_ok=True)
            (legacy_docs / "legacy.md").write_text("legacy doc", encoding="utf-8")
            legacy_user_map.parent.mkdir(parents=True, exist_ok=True)
            legacy_user_map.write_text(json.dumps({"departments": [{"members": [{"id": 1, "name": "Alice"}]}]}), encoding="utf-8")
            legacy_runtime.parent.mkdir(parents=True, exist_ok=True)
            legacy_runtime.write_text(json.dumps({
                "redmine_auth": {"username": "legacy"},
                "sidebar_order": ["test"],
            }), encoding="utf-8")

            class ConfigFactory:
                def for_owner(self, owner_id: str):
                    manager = RedmineConfig(Path.cwd())
                    runtime_path = root / owner_id / "config_runtime.json"
                    runtime_path.parent.mkdir(parents=True, exist_ok=True)
                    manager.runtime_config_path = runtime_path
                    return manager

            redmine_api._USER_REDMINE_SERVICES.clear()
            import features.redmine.users as redmine_users
            fake_settings = type("S", (), {"project_root": root, "data_root": root})()
            with (
                patch.object(redmine_api, "settings", fake_settings),
                patch.object(redmine_users, "settings", fake_settings),
                patch.object(redmine_api, "config_manager", ConfigFactory()),
                patch.object(redmine_api, "DB_PATH", legacy_db),
                patch.object(redmine_api, "DOCS_DIR", legacy_docs),
                patch.object(redmine_api, "USER_MAP_PATH", legacy_user_map),
                patch.object(redmine_users, "USER_MAP_PATH", legacy_user_map),
                patch.object(redmine_api, "owner_db_path", lambda owner: root / owner / "redmine.sqlite3"),
                patch.object(redmine_api, "owner_docs_dir", lambda owner: root / owner / "docs"),
                patch.object(redmine_api, "owner_attachments_dir", lambda owner: root / owner / "attachments"),
                patch.object(redmine_api, "owner_runtime_config_path", lambda owner: root / owner / "config_runtime.json"),
                patch.object(redmine_api, "owner_user_map_path", lambda owner: root / owner / "redmine_user_map.json"),
                # users 模块内部用自己导入的 path 函数落盘，必须一并 patch，
                # 否则测试会向真实 configs/ 写入残留配置。
                patch.object(redmine_users, "owner_runtime_config_path", lambda owner: root / owner / "config_runtime.json"),
                patch.object(redmine_users, "owner_user_map_path", lambda owner: root / owner / "redmine_user_map.json"),
            ):
                service = redmine_api.get_redmine_service_for_owner("alice")

                self.assertEqual(service.repository.get_issue(202)["subject"], "legacy issue")
                self.assertEqual((root / "alice" / "docs" / "legacy.md").read_text(encoding="utf-8"), "legacy doc")
                migrated_runtime = json.loads((root / "alice" / "config_runtime.json").read_text(encoding="utf-8"))
                self.assertEqual(migrated_runtime, {"redmine_auth": {"username": "legacy"}})

    def test_missing_owner_runtime_can_use_static_redmine_credentials(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foundation").mkdir(parents=True)
            configs = root / "configs"
            configs.mkdir(parents=True)
            (configs / "config.json").write_text(
                json.dumps({
                    "redmine": {"base_url": "https://redmine.example.com"},
                    "redmine_auth": {"username": "static-user", "password": "static-secret"},
                }),
                encoding="utf-8",
            )
            manager = RedmineConfig(project_root=root)
            manager.runtime_config_path = root / "data/redmine/by_user/alice/config_runtime.json"

            self.assertEqual(
                manager.load_redmine_credentials(),
                {"username": "static-user", "password": "static-secret"},
            )

    def test_owner_user_map_uses_global_config_path_without_redmine_by_user(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_map = root / "configs" / "redmine_user_map.json"
            forbidden_dir = root / "configs/redmine_by_user"
            global_map.parent.mkdir(parents=True)
            global_map.write_text(
                json.dumps({"departments": [{"department_id": "qa", "department": "QA", "members": [{"id": 1, "name": "Alice"}]}]}),
                encoding="utf-8",
            )
            with (
                patch("features.redmine.users.USER_MAP_PATH", global_map),
            ):
                payload = load_user_map_payload_for_owner("alice")

            self.assertEqual(payload["departments"][0]["department_id"], "qa")
            self.assertFalse(forbidden_dir.exists())


if __name__ == "__main__":
    unittest.main()
