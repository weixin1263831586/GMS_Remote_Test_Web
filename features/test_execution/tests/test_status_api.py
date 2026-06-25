import unittest

from features.test_execution.status_api import _normalize_log_entry


class TestStatusApiLogNormalization(unittest.TestCase):
    def test_old_module_log_string_is_classified_as_module(self):
        entry = _normalize_log_entry(
            "[11:01:41] 06-25 11:01:41 I/ModuleListener: "
            "[1/1] RK3576GMS1 SdcardfsTest#testSdcardfsNotPresent IGNORED"
        )

        self.assertEqual(entry["source"], "module")
        self.assertEqual(entry["type"], "info")
        self.assertIn("ModuleListener", entry["msg"])

    def test_plain_system_log_string_is_classified_as_system(self):
        entry = _normalize_log_entry("[11:01:41] 测试已启动")

        self.assertEqual(entry["source"], "system")
        self.assertEqual(entry["msg"], "测试已启动")

    def test_explicit_source_is_preserved(self):
        entry = _normalize_log_entry(
            {"msg": "Executing command: run_GMS_Test_Auto.sh CTS", "type": "info", "source": "system"}
        )

        self.assertEqual(entry["source"], "system")
