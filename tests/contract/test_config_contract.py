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
            # 契约只针对随源码携带的 example 默认值，保证形状稳定：
            # 部署机上的真实 config.json 是本机数据，其形状可能随部署
            # 漂移（空数组/置空字段），不应参与契约比较。
            shutil.copy2(
                config_manager.config_fallback_path, configs / "config.json"
            )
            isolated = ConfigManager(project_root=project_root)

            self.assertEqual(
                config_shape(isolated.load_config(force_reload=True)),
                read_json('config_shape.json'),
            )
