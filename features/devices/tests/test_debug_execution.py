"""焦点窗口判定兼容 Android 17 的 mFocusedWindow 输出。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.devices import debug_execution
from features.devices.debug_execution import focused_window
from foundation.error_model import ApiError


class FocusedWindowTests(unittest.TestCase):
    ANDROID17_DEFAULT = (
        "WINDOW MANAGER (dumpsys window)\n"
        "  mCurrentFocus=Window{9fd9f57 u0 com.android.vending/com.android.vending.AssetBrowserActivity}\n"
        "  mFocusedApp=ActivityRecord{238062015 u0 com.android.vending/.AssetBrowserActivity t8}\n"
    )

    def test_android17_default_output(self):
        self.assertEqual(focused_window(self.ANDROID17_DEFAULT), (0, "com.android.vending"))

    def test_policy_focused_window_fallback(self):
        output = "    mFocusedWindow=Window{9fd9f57 u0 com.android.vending/com.android.vending.AssetBrowserActivity}\n"
        self.assertEqual(focused_window(output), (0, "com.android.vending"))

    def test_notification_shade_has_no_package(self):
        self.assertIsNone(focused_window("  mCurrentFocus=Window{f7b6ad6 u0 NotificationShade}\n"))

    def test_null_focus(self):
        self.assertIsNone(focused_window("  mCurrentFocus=Window{0 null}\n"))

    def test_secondary_user_is_reported(self):
        output = "  mCurrentFocus=Window{abc123 u10 com.example.app/com.example.app.MainActivity}\n"
        self.assertEqual(focused_window(output), (10, "com.example.app"))

    def test_empty_output(self):
        self.assertIsNone(focused_window(""))

    def test_matching_focus_aliases_are_accepted(self):
        output = (
            "Display: mDisplayId=0\n"
            "mCurrentFocus=Window{a u0 com.example.app/.Main}\n"
            "mFocusedWindow=Window{b u0 com.example.app/.OtherActivity}\n"
        )
        self.assertEqual(focused_window(output), (0, "com.example.app"))

    def test_system_null_conflicting_and_secondary_display_focus_are_rejected(self):
        app = "mFocusedWindow=Window{a u0 com.example.app/.Main}\n"
        unsafe_outputs = [
            "mCurrentFocus=Window{b u0 NotificationShade}\n" + app,
            app + "mCurrentFocus=Window{b u0 NotificationShade}\n",
            "mCurrentFocus=null\n" + app,
            app + "mCurrentFocus=Window{0 null}\n",
            "mCurrentFocus=Window{b u0 com.other.app/.Main}\n" + app,
            "mCurrentFocus=Window{b u10 com.example.app/.Main}\n" + app,
            "mCurrentFocus=Window{b u10 NotificationShade u0 com.example.app/.Main}\n",
            "Display: mDisplayId=1\n" + app,
            "Display #1\n" + app,
            "Display: mDisplayId=0\n" + app + "Display: mDisplayId=1\n" + app,
            "mCurrentFocus=Window{b u0 NotificationShade} mFocusedWindow=Window{a u0 com.example.app/.Main}\n",
        ]
        actions = [
            SimpleNamespace(action="tap", x=10, y=20),
            SimpleNamespace(action="swipe", x=10, y=20, end_x=30, end_y=40, duration_ms=100),
            SimpleNamespace(action="text", text="hello"),
            SimpleNamespace(action="keyevent", key="ENTER"),
        ]
        for output in unsafe_outputs:
            self.assertIsNone(focused_window(output), output)
            for action in actions:
                with self.subTest(output=output, action=action.action), patch.object(
                    debug_execution, "run", return_value=output,
                ) as run:
                    with self.assertRaises(ApiError) as error:
                        debug_execution.perform(object(), {"serial": "SERIAL", "package": "com.example.app"}, action, lambda: None)
                    self.assertEqual(error.exception.status_code, 409)
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(run.call_args.args[1], ["adb", "-s", "SERIAL", "shell", "dumpsys window"])

    def test_approved_input_is_explicitly_bound_to_main_display(self):
        focus = "mCurrentFocus=Window{a u0 com.example.app/.Main}\n"
        actions = [
            (SimpleNamespace(action="tap", x=10, y=20), "tap 10 20"),
            (SimpleNamespace(action="swipe", x=10, y=20, end_x=30, end_y=40, duration_ms=100), "swipe 10 20 30 40 100"),
            (SimpleNamespace(action="text", text="hello world"), "text hello%sworld"),
            (SimpleNamespace(action="keyevent", key="ENTER"), "keyevent KEYCODE_ENTER"),
        ]
        for action, expected in actions:
            with self.subTest(action=action.action), patch.object(debug_execution, "run", side_effect=[focus, ""]) as run:
                result = debug_execution.perform(
                    object(), {"serial": "SERIAL", "package": "com.example.app"}, action, lambda: None,
                )
                self.assertEqual(result["action"], action.action)
                self.assertEqual(run.call_args.args[1], ["adb", "-s", "SERIAL", "shell", "input -d 0 " + expected])


if __name__ == "__main__":
    unittest.main()
