import unittest

from core.config import config_manager
from tests.contract.snapshot_tools import config_shape, read_json


class ConfigContractTests(unittest.TestCase):
    def test_merged_config_shape_matches_contract(self):
        self.assertEqual(
            config_shape(config_manager.load_config(force_reload=True)),
            read_json('config_shape.json'),
        )
