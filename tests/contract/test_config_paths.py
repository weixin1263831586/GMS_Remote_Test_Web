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
    def test_ignores_flat_runtime_config_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "configs/config_runtime.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}", encoding="utf-8")

            self.assertEqual(runtime_config_path(root), root / "data/settings/preferences.json")

    def test_ignores_flat_runtime_environment_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configs/runtime.json"
            configured.parent.mkdir(parents=True)
            configured.write_text("{}", encoding="utf-8")

            self.assertEqual(
                runtime_environment_path(root),
                root / "configs/local/environment.json",
            )

    def test_ignores_flat_automation_profiles_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configs/automation_profiles.json"
            configured.parent.mkdir(parents=True)
            configured.write_text("{}", encoding="utf-8")

            self.assertEqual(automation_profiles_path(root), root / "configs/local/automation_profiles.json")

    def test_owner_config_path_only_uses_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "configs/secrets/redmine/by_user/alice/config_runtime.json"
            legacy = root / "data/redmine/by_user/alice/config_runtime.json"
            # 都缺失 → canonical（写入位置）。
            self.assertEqual(owner_config_path(root, "redmine", "alice"), canonical)
            # 旧位置存在也不改变规范路径。
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}", encoding="utf-8")
            self.assertEqual(owner_config_path(root, "redmine", "alice"), canonical)
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
