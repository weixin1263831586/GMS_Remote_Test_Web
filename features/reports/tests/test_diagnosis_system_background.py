"""报告诊断第 5 路召回（system_background_results）测试（ADR 0014）。

覆盖：payload 键存在、cap 6、source 过滤为 android_internals、
provider 异常不影响其余召回、空 query 短路。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from features.reports import diagnosis_recalls


def _hit(i: int) -> dict:
    return {
        "source": "android_internals",
        "title": f"mechanism {i}",
        "snippet": "…",
        "evidence_level": "background",
        "license": "CC BY-NC-SA 4.0",
    }


class SearchSystemBackgroundTests(unittest.TestCase):
    def test_empty_query_short_circuits(self):
        with patch("features.knowledge.federated_search") as fed:
            self.assertEqual(asyncio.run(diagnosis_recalls.search_system_background("   ")), [])
        fed.assert_not_called()

    def test_caps_at_background_limit(self):
        payload = {"results": [_hit(i) for i in range(20)], "sources_status": []}
        with patch("features.knowledge.federated_search", return_value=payload) as fed:
            out = asyncio.run(diagnosis_recalls.search_system_background("lmkd"))
        self.assertEqual(len(out), diagnosis_recalls.BACKGROUND_LIMIT)
        fed.assert_called_once_with(
            "lmkd", sources=["android_internals"], limit=diagnosis_recalls.BACKGROUND_LIMIT
        )

    def test_provider_error_degrades_to_empty(self):
        with patch("features.knowledge.federated_search", side_effect=RuntimeError("boom")):
            self.assertEqual(asyncio.run(diagnosis_recalls.search_system_background("anr")), [])

    def test_hits_are_background_only(self):
        payload = {"results": [_hit(0)], "sources_status": []}
        with patch("features.knowledge.federated_search", return_value=payload):
            out = asyncio.run(diagnosis_recalls.search_system_background("binder"))
        self.assertEqual(out[0]["evidence_level"], "background")
        self.assertTrue(out[0]["license"])


class DiagnosisPayloadTests(unittest.TestCase):
    def test_diagnosis_source_exposes_background_key(self):
        """编排返回的 payload 必须带 system_background_results（分栏命名）。"""
        import inspect

        import features.reports.analysis_api as api

        source = inspect.getsource(api.diagnose_report_failure)
        self.assertIn('"system_background_results"', source)
        self.assertIn("search_system_background", source)


if __name__ == "__main__":
    unittest.main()
