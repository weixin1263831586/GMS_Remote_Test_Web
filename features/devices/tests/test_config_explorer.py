import unittest
from unittest.mock import patch

from features.devices import config_explorer


class ConfigExplorerTests(unittest.TestCase):
    def test_enabled_overlays_are_grouped_by_target_package(self):
        overlay_output = """
com.android.networkstack
[x] com.rockchip.overlay.networkstack.aosp

android
[x] com.rockchip.overlay.framework.res.common
[ ] com.android.internal.systemui.navbar.threebutton

com.android.phone
[x] com.android.phone.auto_generated_characteristics_rro
"""

        with patch.object(config_explorer, "_adb_path", return_value="adb"), patch.object(
            config_explorer, "run_local_shell_command", return_value=(overlay_output, "", 0)
        ):
            grouped = config_explorer._enabled_overlays_by_target(None)

        self.assertEqual(
            grouped,
            {
                "com.android.networkstack": ["com.rockchip.overlay.networkstack.aosp"],
                "android": ["com.rockchip.overlay.framework.res.common"],
                "com.android.phone": ["com.android.phone.auto_generated_characteristics_rro"],
            },
        )


if __name__ == "__main__":
    unittest.main()
