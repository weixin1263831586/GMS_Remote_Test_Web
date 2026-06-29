import unittest

from features.assistant.intent import resolve


class SuiteModuleIntentTests(unittest.TestCase):
    def test_camera_related_modules_routes_to_suite_module_search(self):
        intent = resolve("Camera相关测试项有哪些模块", {})

        self.assertEqual(intent.tool_name, "suite_modules")
        self.assertEqual(intent.params["query"], "Camera")
        self.assertFalse(intent.needs_confirm)


if __name__ == "__main__":
    unittest.main()
