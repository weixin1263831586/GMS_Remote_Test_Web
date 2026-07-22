import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from features.auth import CurrentUser
from features.reports import files_api
from features.reports.files_api import (
    _decorate_report_for_client,
    _is_registered_report_file_path,
    download_report,
    list_reports,
)


CURRENT_USER = CurrentUser(
    id="NqWo58sh1jr5c6ZiyxxPtQ",
    username="hcq",
    role="user",
)


class ReportFilesApiTests(unittest.TestCase):
    def test_report_file_preview_reads_registered_local_file_without_ssh(self):
        with TemporaryDirectory() as tmp:
            result_dir = Path(tmp) / "android-cts" / "results" / "2026.07.20_12.00.00"
            result_dir.mkdir(parents=True)
            report_file = result_dir / "test_result.html"
            report_file.write_text("<html>本机报告</html>", encoding="utf-8")
            report = {
                "timestamp": result_dir.name,
                "result_dir": str(result_dir),
                "owner_id": CURRENT_USER.id,
            }
            fake_db = type(
                "FakeDb",
                (),
                {
                    "get_reports": lambda self, limit=500, owner_id=None, include_all=False: [report],
                },
            )()
            request = SimpleNamespace(state=SimpleNamespace(current_user=CURRENT_USER))

            with patch("features.reports.files_api.test_report_db", fake_db), patch(
                "features.reports.files_api.get_accessible_report_by_timestamp",
                return_value=report,
            ), patch(
                "features.reports.files_api.can_access_report",
                return_value=True,
            ), patch.object(files_api.dependencies, "file_utils", object()), patch.object(
                files_api.dependencies, "ssh_manager", None
            ):
                response = asyncio.run(download_report(
                    request,
                    report_id=None,
                    report_timestamp=result_dir.name,
                    download=False,
                    file=report_file.name,
                    path=None,
                ))

            payload = json.loads(response.body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["content"], "<html>本机报告</html>")
            self.assertEqual(payload["content_type"], "text/html")

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
                    "get_reports": lambda self, limit=500, owner_id=None, include_all=False: [
                        {
                            "timestamp": result_dir.name,
                            "result_dir": str(result_dir),
                            "owner_id": CURRENT_USER.id,
                        }
                    ]
                },
            )()

            request = SimpleNamespace(
                state=SimpleNamespace(current_user=CURRENT_USER)
            )
            with patch("features.reports.files_api.test_report_db", fake_db):
                self.assertTrue(_is_registered_report_file_path(str(report_file), request))
                self.assertTrue(_is_registered_report_file_path(str(log_file), request))
                self.assertFalse(_is_registered_report_file_path(str(root / "other.txt"), request))

    def test_list_reports_user_only_matches_platform_id_and_returns_display_client(self):
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.16.14.66"))

        fake_db = type(
            "FakeDb",
            (),
            {
                "get_reports": lambda self, limit=50, owner_id=None, include_all=False: [
                    {"timestamp": "1", "owner_id": "NqWo58sh1jr5c6ZiyxxPtQ"},
                    {"timestamp": "2", "owner_id": "other-user"},
                ][:limit] if owner_id is None else [
                    item for item in [
                        {"timestamp": "1", "owner_id": "NqWo58sh1jr5c6ZiyxxPtQ"},
                        {"timestamp": "2", "owner_id": "other-user"},
                    ] if item["owner_id"] == owner_id
                ][:limit]
            },
        )()

        with patch("features.reports.files_api.test_report_db", fake_db), patch(
            "features.reports.access.get_authenticated_user",
            return_value=CURRENT_USER,
        ), patch(
            "features.reports.files_api.require_authenticated_user",
            return_value=CURRENT_USER,
        ):
            response = asyncio.run(list_reports(request, user_only=True))

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(len(payload["reports"]), 1)
        self.assertEqual(payload["reports"][0]["display_client_id"], "hcq")
        self.assertNotIn("owner_id", payload["reports"][0])

    def test_report_display_client_uses_current_principal_display(self):
        report = _decorate_report_for_client(
            {"owner_id": "NqWo58sh1jr5c6ZiyxxPtQ"}, "hcq"
        )
        self.assertEqual(report["display_client_id"], "hcq")

    def test_list_reports_filters_exact_automation_job_attempt(self):
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        reports = [
            {
                "timestamp": "cluster-job-1", "cluster_job_id": "job-1",
                "attempt_id": "attempt-1", "automation_run_id": "ats-1",
                "owner_id": "NqWo58sh1jr5c6ZiyxxPtQ",
            },
            {
                "timestamp": "cluster-job-2", "cluster_job_id": "job-2",
                "attempt_id": "attempt-2", "automation_run_id": "ats-2",
                "owner_id": "other-user",
            },
        ]
        fake_db = type(
            "FakeDb",
            (),
            {
                "get_reports": lambda self, limit=500, owner_id=None, include_all=False: [
                    item for item in reports
                    if owner_id is None or item["owner_id"] == owner_id
                ][:limit],
                "get_report_by_timestamp": lambda self, timestamp, owner_id=None, include_all=False: next(
                    (item for item in reports if item["timestamp"] == timestamp and (include_all or item["owner_id"] == owner_id)), None
                ),
            },
        )()

        with patch("features.reports.files_api.test_report_db", fake_db), patch(
            "features.reports.access.get_authenticated_user",
            return_value=CURRENT_USER,
        ), patch(
            "features.reports.files_api.require_authenticated_user",
            return_value=CURRENT_USER,
        ):
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
