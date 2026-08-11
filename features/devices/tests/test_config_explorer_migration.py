import unittest
from pathlib import Path

from app import app


class DeviceConfigExplorerMigrationTests(unittest.TestCase):
    def test_config_explorer_routes_are_registered(self):
        paths = {route.path for route in app.routes}

        self.assertIn("/api/config-explorer", paths)
        self.assertIn("/api/config-explorer/packages-with-path", paths)
        self.assertIn("/api/config-explorer/features", paths)
        self.assertIn("/api/config-explorer/props", paths)
        self.assertIn("/api/config-explorer/decompile", paths)

    def test_device_management_ui_exposes_device_info_modal(self):
        shell = Path("web/shell/shell.html").read_text(encoding="utf-8")
        navigation = Path("web/static/js/navigation.js").read_text(encoding="utf-8")
        combined = shell + "\n" + navigation

        self.assertIn('id="device-config-modal"', shell)
        self.assertIn('const actionDeviceId = String(device.device_id || serialNo)', combined)
        self.assertIn('const serialAttr = escapeIconAttr(actionDeviceId)', combined)
        self.assertIn('data-serial="${serialAttr}"', combined)
        self.assertIn(
            'openDeviceConfigExplorer(this.dataset.serial,this.dataset.worker)',
            combined,
        )
        for field in (
            'serial_no',
            'source_host',
            'model',
            'soc_model',
            'android_version',
            'locked_by',
        ):
            self.assertIn(f'escapeHtml(String(device.{field}', shell)
        self.assertIn("function openDeviceConfigExplorer", combined)
        self.assertIn("/api/config-explorer/packages-with-path", combined)
        self.assertIn("/api/config-explorer/decompile", combined)


if __name__ == "__main__":
    unittest.main()
