import shutil
import tempfile
import unittest
from pathlib import Path

from foundation.config import ConfigManager, config_manager
from tests.contract.snapshot_tools import config_shape, read_json


class ConfigContractTests(unittest.TestCase):
    def test_merged_config_shape_matches_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            configs = project_root / "configs"
            configs.mkdir()
            (project_root / "foundation").mkdir()
            shutil.copy2(config_manager.config_path, configs / "config.json")
            isolated = ConfigManager(project_root=project_root)

            self.assertEqual(
                config_shape(isolated.load_config(force_reload=True)),
                read_json('config_shape.json'),
            )
