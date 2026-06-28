"""Tests for certification error rule detection (§5.3)."""

from __future__ import annotations

import unittest

from features.redmine.cert_rules import detect_certification_errors


class CertRulesTests(unittest.TestCase):
    def test_vbmeta_test_key_detected(self):
        text = "The partition 'system' is signed with a publicly known VBMeta test key '22de39...'"
        r = detect_certification_errors(text)
        self.assertIn("VBMeta test key", r["errors"])
        self.assertIn("system", r["partitions"])
        self.assertEqual(r["certification_type"], "")
        self.assertTrue(r["failures"])
        self.assertEqual(r["failures"][0]["module"], "AVB/VBMeta")

    def test_bts_certification_type(self):
        text = "BTS scan: vbmeta signed with VBMeta test key"
        r = detect_certification_errors(text)
        self.assertEqual(r["certification_type"], "BTS")
        self.assertIn("vbmeta", r["partitions"])

    def test_apex_signature(self):
        text = "APEX 包签名 hash mismatch on system"
        r = detect_certification_errors(text)
        self.assertIn("APEX signature", r["errors"])

    def test_multiple_partitions(self):
        text = "boot, system, vendor and vbmeta all flagged"
        r = detect_certification_errors(text)
        for p in ("boot", "system", "vendor", "vbmeta"):
            self.assertIn(p, r["partitions"])

    def test_empty_and_no_match_safe(self):
        self.assertEqual(detect_certification_errors(""), {"errors": [], "partitions": [], "certification_type": "", "failures": []})
        r = detect_certification_errors("普通文本无报错")
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["failures"], [])

    def test_failure_reason_carries_context(self):
        text = "前导文字 The partition 'system' is signed with a publicly known VBMeta test key 后续说明"
        r = detect_certification_errors(text)
        reason = r["failures"][0]["reason"]
        self.assertIn("VBMeta test key", reason)


if __name__ == "__main__":
    unittest.main()
