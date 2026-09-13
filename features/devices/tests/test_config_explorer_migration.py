import unittest
from pathlib import Path

from app import app


class DeviceConfigExplorerMigrationTests(unittest.TestCase):
    def test_config_explorer_routes_are_registered(self):
        from foundation.routing_introspection import flatten_app_routes

        paths = {route.path for route in flatten_app_routes(app)}

        self.assertIn("/api/config-explorer", paths)
        self.assertIn("/api/config-explorer/packages-with-path", paths)
        self.assertIn("/api/config-explorer/features", paths)
        self.assertIn("/api/config-explorer/props", paths)
        self.assertIn("/api/config-explorer/decompile", paths)

    def test_device_management_ui_exposes_device_info_modal(self):
        # CSP 前置迁移后 shell 主脚本外置到 web/static/js/shell/shell-*.js。
        shell_parts = [Path("web/shell/shell.html").read_text(encoding="utf-8")]
        shell_parts += [
            p.read_text(encoding="utf-8")
            for p in sorted(Path("web/static/js/shell").glob("*.js"))
        ]
        shell = "\n".join(shell_parts)
        navigation = Path("web/static/js/navigation.js").read_text(encoding="utf-8")
        combined = shell + "\n" + navigation

        self.assertIn('id="device-config-modal"', shell)
        self.assertIn('const actionDeviceId = String(device.device_id || serialNo)', combined)
        self.assertIn('const serialAttr = escapeIconAttr(actionDeviceId)', combined)
        self.assertIn('data-serial="${serialAttr}"', combined)
        # inline onclick 已迁移为 act-bridge data-* 声明。
        self.assertIn(
            'data-click="openDeviceConfigExplorer"', combined
        )
        self.assertIn('data-r0="dataset.serial"', combined)
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
