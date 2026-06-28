"""Tests for RedmineCaseExtractor structured field extraction."""

from __future__ import annotations

import unittest

from features.redmine.case_extractor import RedmineCaseExtractor


VBMETA_ISSUE = {
    "issue_id": 633454,
    "subject": "[RK3576] BTS issue system signed with VBMeta test key",
    "description": "现在BTS提示The partition 'vbmeta' is signed with a publicly known VBMeta test key 请问怎么更新AVB key",
    "status_name": "Confirmed",
    "category": "VTS",
    "fixed_version": "RK3576_ANDROID16.0",
    "failures_json": [
        {
            "module": "BTS",
            "name": "vbmeta test key",
            "reason": "The partition 'system' is signed with a publicly known VBMeta test key",
        }
    ],
    "journals_json": [],
    "attachments_json": [],
}

POWER_AIDL_ISSUE = {
    "issue_id": 598972,
    "subject": "RK3576 Android16 VtsHalPowerTargetTest模块PowerAidl#hasFixedPerformance",
    "description": (
        "run vts -m VtsHalPowerTargetTest -t "
        "Power/PowerAidl#hasFixedPerformance/0_android_hardware_power_IPower_default 测试fail\n"
        "hardware/interfaces/power/aidl/vts/VtsHalPowerTargetTest.cpp:359: Failure\n"
        "Value of: supported\n"
        "  Actual: false\n"
        "Expected: true"
    ),
    "status_name": "Closed",
    "fixed_version": "RK3576_Android14.0_Express_SDK",
    "journals_json": [],
    "attachments_json": [],
    "failures_json": [],
}


class CaseExtractorTests(unittest.TestCase):
    def test_vbmeta_extracts_module_and_signature(self):
        fact = RedmineCaseExtractor.extract(VBMETA_ISSUE)
        self.assertEqual(fact["chip_platform"], "RK3576")
        self.assertEqual(fact["android_version"], "Android16")
        self.assertEqual(fact["certification_type"], "BTS")
        self.assertEqual(fact["module"], "AVB/VBMeta")
        self.assertEqual(fact["error_signature"], "VBMeta test key")
        self.assertIn("VBMeta test key", fact["problem_summary"])
        self.assertIn("vbmeta", [k.lower() for k in fact["keywords"]])
        self.assertGreater(fact["confidence"], 50)

    def test_vbmeta_gets_root_cause_and_solution_when_missing(self):
        issue = {**VBMETA_ISSUE, "error_analysis": "", "solution": ""}
        fact = RedmineCaseExtractor.extract(issue)
        self.assertIn("production", fact["root_cause"])
        self.assertIn("production", fact["solution"])
        self.assertIn("BTS", fact["verification"])

    def test_power_hal_module_detected(self):
        issue = {
            "issue_id": 635224,
            "subject": "RK3576 Android16 VtsHalPowerTargetTest模块PowerAidl#hasFixedPerformance",
            "description": "",
            "fixed_version": "ANDROID16",
        }
        fact = RedmineCaseExtractor.extract(issue)
        self.assertEqual(fact["module"], "Power HAL")
        self.assertEqual(fact["chip_platform"], "RK3576")
        self.assertEqual(fact["android_version"], "Android16")

    def test_power_aidl_fixed_performance_gets_gms_like_fields(self):
        fact = RedmineCaseExtractor.extract(POWER_AIDL_ISSUE)
        self.assertEqual(fact["certification_type"], "VTS")
        self.assertEqual(fact["module"], "Power HAL")
        self.assertEqual(fact["error_signature"], "PowerAidl hasFixedPerformance unsupported")
        self.assertIn("Power/PowerAidl#hasFixedPerformance", "\n".join(fact["symptoms"]))
        self.assertIn("supported=false", "\n".join(fact["symptoms"]))
        self.assertIn("Mode::FIXED_PERFORMANCE", fact["root_cause"])
        self.assertIn("isModeSupported", fact["solution"])
        self.assertIn("VtsHalPowerTargetTest", fact["verification"])
        self.assertGreaterEqual(fact["confidence"], 85)

    def test_reply_template_includes_module_and_signature(self):
        fact = RedmineCaseExtractor.extract(VBMETA_ISSUE)
        reply = fact["reply_template"]
        self.assertIn("#633454", reply)
        self.assertIn("AVB/VBMeta", reply)
        self.assertIn("VBMeta test key", reply)

    def test_high_confidence_when_closed_with_solution(self):
        issue = {**VBMETA_ISSUE, "status_name": "Closed", "solution": "重新签名"}
        fact = RedmineCaseExtractor.extract(issue)
        self.assertEqual(fact["source_quality"], "high")

    def test_no_ai_call_and_empty_text_safe(self):
        fact = RedmineCaseExtractor.extract({"issue_id": 1, "subject": "普通咨询", "description": "请问一下"})
        self.assertEqual(fact["module"], "")
        self.assertEqual(fact["error_signature"], "")
        self.assertEqual(fact["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
