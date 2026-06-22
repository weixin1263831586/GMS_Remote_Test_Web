import unittest

from tests.contract.snapshot_tools import read_json, ui_controls, ui_source_groups


class UiContractTests(unittest.TestCase):
    def test_control_ids_and_handlers_match_contract(self):
        self.assertEqual(
            ui_controls(ui_source_groups()),
            read_json('ui_controls.json'),
        )
