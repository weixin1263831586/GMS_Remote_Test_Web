import tempfile
import unittest
from pathlib import Path

from foundation.config_paths import (
    automation_profiles_path,
    owner_config_path,
    runtime_config_path,
    runtime_environment_path,
    sanitize_owner_id,
)


class ConfigPathContractTests(unittest.TestCase):
    def test_uses_flat_runtime_config_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "configs/config_runtime.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}", encoding="utf-8")

            self.assertEqual(runtime_config_path(root), canonical)

    def test_uses_flat_runtime_environment_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configs/runtime.json"
            configured.parent.mkdir(parents=True)
            configured.write_text("{}", encoding="utf-8")

            self.assertEqual(
                runtime_environment_path(root),
                root / "configs/runtime.json",
            )

    def test_uses_primary_automation_profiles_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configs/automation_profiles.json"
            configured.parent.mkdir(parents=True)
            configured.write_text("{}", encoding="utf-8")

            self.assertEqual(automation_profiles_path(root), configured)

    def test_owner_config_path_defaults_to_secrets_and_falls_back_to_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "configs/secrets/redmine/by_user/alice/config_runtime.json"
            legacy = root / "data/redmine/by_user/alice/config_runtime.json"
            # 都缺失 → canonical（写入位置）。
            self.assertEqual(owner_config_path(root, "redmine", "alice"), canonical)
            # 仅 legacy 存在 → 迁移期回退读 legacy。
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}", encoding="utf-8")
            self.assertEqual(owner_config_path(root, "redmine", "alice"), legacy)
            # canonical 出现后优先 canonical。
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}", encoding="utf-8")
            self.assertEqual(owner_config_path(root, "redmine", "alice"), canonical)

    def test_owner_config_path_sanitizes_owner_id(self):
        self.assertEqual(sanitize_owner_id(""), "anonymous")
        self.assertEqual(sanitize_owner_id(None), "anonymous")
        self.assertEqual(sanitize_owner_id("a/b..c"), "a_b__c")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = owner_config_path(root, "gerrit", "../../etc")
            self.assertEqual(path.parent.name, "______etc")
            self.assertEqual(path.parent.parent.name, "by_user")


if __name__ == "__main__":
    unittest.main()
