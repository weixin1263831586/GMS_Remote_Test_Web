"""Tests for cross-module reverse relations (reports/redmine aggregation).

Stubs test_report_db and redmine service so no real DB/network is needed.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from features.notes import relations


class ParseRelatedModuleTests(unittest.TestCase):
    def test_split_module_and_test_case(self) -> None:
        self.assertEqual(relations.parse_related_module("Camera::testOpen"), ("Camera", "testOpen"))

    def test_no_separator_returns_module_only(self) -> None:
        self.assertEqual(relations.parse_related_module("WiFi"), ("WiFi", ""))

    def test_empty(self) -> None:
        self.assertEqual(relations.parse_related_module(""), ("", ""))
        self.assertEqual(relations.parse_related_module(None), ("", ""))


class FindRelatedReportsTests(unittest.TestCase):
    def test_filters_by_test_module_and_maps_fields(self) -> None:
        fake_reports = [
            {
                "timestamp": "2026-07-03T10:00:00",
                "test_type": "GTS",
                "test_module": "CameraModule",
                "test_case": "testOpen",
                "devices": ["serial1"],
                "result_dir": "/r/1",
                "status": "fail",
            },
            {
                "timestamp": "2026-07-03T11:00:00",
                "test_type": "CTS",
                "test_module": "WiFiModule",
                "test_case": "testScan",
                "devices": [],
                "result_dir": "/r/2",
                "status": "pass",
            },
        ]
        # 临时 stub features.reports.repository.test_report_db
        fake_repo_mod = types.ModuleType("features.reports.repository")
        fake_db = mock.MagicMock()
        fake_db.get_reports.return_value = fake_reports
        fake_repo_mod.test_report_db = fake_db
        sys.modules["features.reports.repository"] = fake_repo_mod
        try:
            results = relations.find_related_reports("CameraModule", "")
        finally:
            sys.modules.pop("features.reports.repository", None)

        self.assertEqual(len(results), 1)
        only = results[0]
        self.assertEqual(only["test_module"], "CameraModule")
        self.assertEqual(only["test_case"], "testOpen")
        self.assertEqual(only["source"], "report")
        self.assertEqual(only["result_dir"], "/r/1")

    def test_empty_module_returns_empty(self) -> None:
        self.assertEqual(relations.find_related_reports("", "x"), [])

    def test_exception_is_swallowed(self) -> None:
        bad_repo = types.ModuleType("features.reports.repository")
        bad_db = mock.MagicMock()
        bad_db.get_reports.side_effect = RuntimeError("boom")
        bad_repo.test_report_db = bad_db
        sys.modules["features.reports.repository"] = bad_repo
        try:
            self.assertEqual(relations.find_related_reports("M", ""), [])
        finally:
            sys.modules.pop("features.reports.repository", None)


class FindRelatedRedmineCasesTests(unittest.TestCase):
    def test_filters_by_module_and_decodes_solution_json(self) -> None:
        cases = [
            {
                "case_id": "c1",
                "title": "VBMeta test key",
                "module": "CameraModule",
                "chip_platform": "RK3576",
                "android_version": "16",
                "canonical_error_signature": "sig",
                "solution_json": '{"root_cause":"k","fix":"v"}',
                "source_issue_ids_json": "[111, 222]",
                "status": "mature",
            },
            {
                "case_id": "c2",
                "title": "unrelated",
                "module": "OtherModule",
                "solution_json": "{}",
                "source_issue_ids_json": "[]",
                "status": "mature",
            },
        ]
        fake_service = mock.MagicMock()
        fake_service.knowledge.list_mature_cases.return_value = {"items": cases, "total": 2}

        redmine_api = types.ModuleType("features.redmine.api")
        redmine_api.get_redmine_service_for_request = mock.MagicMock(return_value=fake_service)
        sys.modules["features.redmine.api"] = redmine_api
        try:
            results = relations.find_related_redmine_cases(request=object(), module="CameraModule")
        finally:
            sys.modules.pop("features.redmine.api", None)

        self.assertEqual(len(results), 1)
        only = results[0]
        self.assertEqual(only["case_id"], "c1")
        self.assertEqual(only["module"], "CameraModule")
        self.assertEqual(only["solution"], {"root_cause": "k", "fix": "v"})
        self.assertEqual(only["source_issue_ids"], [111, 222])
        self.assertEqual(only["source"], "redmine_case")

    def test_empty_module_returns_empty(self) -> None:
        self.assertEqual(relations.find_related_redmine_cases(request=object(), module=""), [])


class BuildRelatedTests(unittest.TestCase):
    def test_build_related_aggregates_both_sources(self) -> None:
        with mock.patch.object(relations, "find_related_reports", return_value=[{"x": 1}]), \
             mock.patch.object(relations, "find_related_redmine_cases", return_value=[{"y": 2}]):
            result = relations.build_related(request=object(), related_module="M::t", note_id="n1")
        self.assertEqual(result["module"], "M")
        self.assertEqual(result["test_case"], "t")
        self.assertEqual(len(result["reports"]), 1)
        self.assertEqual(len(result["redmine_cases"]), 1)
        self.assertEqual(result["total"], 2)


if __name__ == "__main__":
    unittest.main()
