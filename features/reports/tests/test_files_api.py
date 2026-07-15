import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from features.reports.files_api import _decorate_report_for_client, _is_registered_report_file_path, list_reports


class ReportFilesApiTests(unittest.TestCase):
    def test_registered_report_file_path_allows_result_and_logs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_dir = root / "android-cts" / "results" / "2026.06.30_10.00.00"
            result_dir.mkdir(parents=True)
            report_file = result_dir / "test_result.xml"
            report_file.write_text("<Result />", encoding="utf-8")
            log_dir = root / "android-cts" / "logs" / result_dir.name
            log_dir.mkdir(parents=True)
            log_file = log_dir / "host_log.txt"
            log_file.write_text("ok", encoding="utf-8")

            fake_db = type(
                "FakeDb",
                (),
                {
                    "get_reports": lambda self, limit=500: [
                        {"timestamp": result_dir.name, "result_dir": str(result_dir)}
                    ]
                },
            )()

            with patch("features.reports.files_api.test_report_db", fake_db):
                self.assertTrue(_is_registered_report_file_path(str(report_file)))
                self.assertTrue(_is_registered_report_file_path(str(log_file)))
                self.assertFalse(_is_registered_report_file_path(str(root / "other.txt")))

    def test_list_reports_user_only_matches_platform_id_and_returns_display_client(self):
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.16.14.66"))

        fake_db = type(
            "FakeDb",
            (),
            {
                "get_reports": lambda self, limit=50, user_only=None: [
                    {"timestamp": "1", "client_id": "NqWo58sh1jr5c6ZiyxxPtQ"},
                    {"timestamp": "2", "client_id": "other-user"},
                ][:limit]
            },
        )()

        with patch("features.reports.files_api.test_report_db", fake_db), patch(
            "features.reports.files_api._user_id_from_request",
            lambda _request: "NqWo58sh1jr5c6ZiyxxPtQ",
        ), patch(
            "features.reports.files_api._user_display_id_from_request",
            lambda _request: "hcq@172.16.14.66",
        ):
            response = asyncio.run(list_reports(request, user_only=True))

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(len(payload["reports"]), 1)
        self.assertEqual(payload["reports"][0]["display_client_id"], "hcq@172.16.14.66")

    def test_report_display_client_uses_current_display_for_legacy_platform_id(self):
        display, aliases = "hcq@172.16.14.66", {"NqWo58sh1jr5c6ZiyxxPtQ", "hcq@172.16.14.66"}
        report = _decorate_report_for_client({"client_id": "NqWo58sh1jr5c6ZiyxxPtQ"}, display, aliases)
        self.assertEqual(report["display_client_id"], "hcq@172.16.14.66")

    def test_list_reports_filters_exact_automation_job_attempt(self):
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        reports = [
            {
                "timestamp": "cluster-job-1", "cluster_job_id": "job-1",
                "attempt_id": "attempt-1", "automation_run_id": "ats-1",
            },
            {
                "timestamp": "cluster-job-2", "cluster_job_id": "job-2",
                "attempt_id": "attempt-2", "automation_run_id": "ats-2",
            },
        ]
        fake_db = type(
            "FakeDb",
            (),
            {
                "get_reports": lambda self, limit=500, user_only=None: reports[:limit],
                "get_report_by_timestamp": lambda self, timestamp: next(
                    (item for item in reports if item["timestamp"] == timestamp), None
                ),
            },
        )()

        with patch("features.reports.files_api.test_report_db", fake_db):
            response = asyncio.run(list_reports(
                request,
                cluster_job_id="job-1",
                attempt_id="attempt-1",
                automation_run_id="ats-1",
            ))

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            [item["timestamp"] for item in payload["reports"]], ["cluster-job-1"]
        )


if __name__ == "__main__":
    unittest.main()
