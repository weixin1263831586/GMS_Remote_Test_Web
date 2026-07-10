import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FeatureImportTests(unittest.TestCase):
    def assert_import_order(self, *modules: str) -> None:
        statement = '; '.join(f'import {module}' for module in modules)
        result = subprocess.run(
            [sys.executable, '-c', statement],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reports_can_load_before_test_execution(self):
        self.assert_import_order('features.reports', 'features.test_execution')

    def test_test_execution_can_load_before_reports(self):
        self.assert_import_order('features.test_execution', 'features.reports')

    def test_devices_can_load_before_system(self):
        self.assert_import_order('features.devices', 'features.system')

    def test_system_can_load_before_devices(self):
        self.assert_import_order('features.system', 'features.devices')


if __name__ == '__main__':
    unittest.main()
