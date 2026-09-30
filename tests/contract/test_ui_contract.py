import unittest

from tests.contract.snapshot_tools import read_json, ui_controls, ui_source_groups


class UiContractTests(unittest.TestCase):
    def test_gerrit_contract_includes_rendered_controls(self):
        controls = ui_controls(ui_source_groups())["gerrit-dashboard"]
        self.assertTrue(
            {"owner", "personalProfile", "departmentProfile"} <= set(controls["ids"])
        )
        self.assertTrue(
            {"saveChangeToWiki", "setTrendStartDate", "showGerritTrendDetail"}
            <= set(controls["handlers"])
        )

    def test_control_ids_and_handlers_match_contract(self):
        self.assertEqual(
            ui_controls(ui_source_groups()),
            read_json('ui_controls.json'),
        )
