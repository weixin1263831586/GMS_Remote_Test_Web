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
            "lmkd",
            sources=["android_internals"],
            limit=diagnosis_recalls.BACKGROUND_LIMIT,
            android_api_level=None,
        )

    def test_android_api_level_is_forwarded(self):
        """版本上下文透传：报告侧统一转换的 API level 必须进入联邦检索。"""
        payload = {"results": [_hit(0)], "sources_status": []}
        with patch("features.knowledge.federated_search", return_value=payload) as fed:
            out = asyncio.run(
                diagnosis_recalls.search_system_background("lmkd", android_api_level=36)
            )
        self.assertEqual(len(out), 1)
        fed.assert_called_once_with(
            "lmkd",
            sources=["android_internals"],
            limit=diagnosis_recalls.BACKGROUND_LIMIT,
            android_api_level=36,
        )

    def test_android_version_reaches_anchor_verification(self):
        payload = {
            "results": [{
                **_hit(0),
                "source_anchors": [{"repo": "platform/frameworks/base", "path": "a/b/C.java"}],
            }],
            "sources_status": [],
        }
        with patch("features.knowledge.federated_search", return_value=payload), patch(
            "features.reports.anchor_verification.verify_background_anchors",
            return_value=[],
        ) as verify:
            asyncio.run(
                diagnosis_recalls.search_system_background(
                    "lmkd", android_api_level=37
                )
            )
        verify.assert_called_once_with(payload["results"], android_version="17")

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

    def test_diagnosis_forwards_api_level_from_request(self):
        """编排必须把报告上下文的 API level 传给第五路召回（版本贯通回归）。"""
        import inspect

        import features.reports.analysis_api as api

        source = inspect.getsource(api.diagnose_report_failure)
        self.assertIn("android_api_level_from_request", source)


class AndroidApiLevelFromRequestTests(unittest.TestCase):
    """suite_version / android_version → API level 统一转换。"""

    def _request(self, **fields):
        from features.reports.api_models import ReportDiagnosisRequest

        return ReportDiagnosisRequest(test_name="t", **fields)

    def test_suite_version_major_maps_to_api_level(self):
        from features.reports.knowledge_ranking import android_api_level_from_request

        for suite, expected in (
            ("16.0_r1", 36), ("android-15", 35), ("14", 34), ("13.0_r7", 33),
        ):
            with self.subTest(suite=suite):
                self.assertEqual(
                    android_api_level_from_request(self._request(suite_version=suite)),
                    expected,
                )

    def test_android_version_field_used_when_suite_missing(self):
        from features.reports.knowledge_ranking import android_api_level_from_request

        request = self._request(android_version="16")
        self.assertEqual(android_api_level_from_request(request), 36)

    def test_unknown_version_returns_none(self):
        from features.reports.knowledge_ranking import android_api_level_from_request

        self.assertIsNone(
            android_api_level_from_request(self._request(suite_version="12", android_version=""))
        )
        self.assertIsNone(android_api_level_from_request(self._request()))

    def test_api_level_maps_back_to_android_major(self):
        from features.reports.knowledge_ranking import android_version_from_api_level

        self.assertEqual(android_version_from_api_level(37), "17")
        self.assertEqual(android_version_from_api_level(36), "16")
        self.assertEqual(android_version_from_api_level(None), "")


if __name__ == "__main__":
    unittest.main()
