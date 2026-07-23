import tempfile
import unittest
from pathlib import Path

from foundation.config_paths import (
    automation_profiles_path,
    runtime_config_path,
    runtime_environment_path,
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


if __name__ == "__main__":
    unittest.main()
