import asyncio
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from features.auth import CurrentUser
from features.cluster import index_cluster_report
from features.reports import files_api
from features.reports.downloads import (
    create_remote_report_bundle,
    merge_remote_report_exports,
    remove_report_bundle,
)
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
    def test_missing_report_directory_is_not_exposed_in_response(self):
        missing = "/srv/customer-secret/results/report-1"
        report = {
            "report_id": "report-1",
            "timestamp": "report-1",
            "result_dir": missing,
            "owner_id": CURRENT_USER.id,
        }
        fake_db = SimpleNamespace(get_report=lambda *args, **kwargs: report)
        request = SimpleNamespace(
            state=SimpleNamespace(current_user=CURRENT_USER)
        )

        with self.assertLogs(files_api.logger, level="ERROR") as captured, patch(
            "features.reports.files_api.test_report_db", fake_db
        ), patch(
            "features.reports.files_api.can_access_report", return_value=True
        ), patch.object(files_api.dependencies, "file_utils", object()):
            response = asyncio.run(download_report(
                request,
                report_id="report-1",
                report_timestamp=None,
                download=False,
                file=None,
                path=None,
            ))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(missing, response.body.decode("utf-8"))
        self.assertNotIn(missing, "\n".join(captured.output))

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

    def test_cluster_report_download_uses_tradefed_report_name(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_root = root / "android-gts"
            run_folder = "2026.07.31_18.04.00.815_7887"
            result_dir = suite_root / "results" / run_folder
            logs_dir = suite_root / "logs" / run_folder
            (suite_root / "tools").mkdir(parents=True)
            result_dir.mkdir(parents=True)
            logs_dir.mkdir(parents=True)
            (result_dir / "test_result.xml").write_text(
                "<Result />", encoding="utf-8"
            )
            (logs_dir / "host_log.txt").write_text("log", encoding="utf-8")
            report = {
                "report_id": "cluster:job-1:attempt-1",
                "timestamp": "cluster-job-1",
                "report_name": run_folder,
                "source_timestamp": run_folder,
                "result_dir": str(result_dir),
                "suite_path": str(suite_root / "tools"),
                "worker_id": "ats-worker-controller",
                "owner_id": CURRENT_USER.id,
            }
            fake_db = SimpleNamespace(
                get_report=lambda *args, **kwargs: report,
            )
            request = SimpleNamespace(
                state=SimpleNamespace(current_user=CURRENT_USER)
            )

            with patch("features.reports.files_api.test_report_db", fake_db), patch(
                "features.reports.files_api.can_access_report",
                return_value=True,
            ):
                response = asyncio.run(download_report(
                    request,
                    report_id=report["report_id"],
                    report_timestamp=None,
                    download=True,
                    file=None,
                    path=None,
                ))

        expected = "2026.07.31_18.04.00.815_7887.zip"
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'filename="{expected}"', response.headers["content-disposition"])
        bundle_path = Path(response.path)
        try:
            with zipfile.ZipFile(bundle_path) as bundle:
                self.assertEqual(
                    set(bundle.namelist()),
                    {
                        f"results/{run_folder}/test_result.xml",
                        f"logs/{run_folder}/host_log.txt",
                    },
                )
        finally:
            remove_report_bundle(bundle_path)

    def test_remote_report_exports_are_merged_under_results_and_logs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            results_export = root / "results.zip"
            logs_export = root / "logs.zip"
            run_folder = "2026.07.31_18.04.00.815_7887"
            with zipfile.ZipFile(results_export, "w") as archive:
                archive.writestr(f"{run_folder}/test_result.xml", "result")
                archive.writestr("../escape.txt", "ignored")
            with zipfile.ZipFile(logs_export, "w") as archive:
                archive.writestr(f"{run_folder}/inv_1/host_log.txt", "log")

            bundle = merge_remote_report_exports({
                "results": results_export,
                "logs": logs_export,
            })

        self.assertIsNotNone(bundle)
        try:
            with zipfile.ZipFile(bundle.path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        f"results/{run_folder}/test_result.xml",
                        f"logs/{run_folder}/inv_1/host_log.txt",
                    },
                )
        finally:
            remove_report_bundle(bundle.path)

    def test_remote_report_download_uses_existing_worker_export_channel(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            transfer_root = root / "transfers"
            transfer_root.mkdir()
            run_folder = "2026.07.31_18.04.00.815_7887"
            for kind, filename in (("results", "test_result.xml"), ("logs", "host_log.txt")):
                with zipfile.ZipFile(transfer_root / f"{kind}.zip", "w") as archive:
                    archive.writestr(f"{run_folder}/{filename}", kind)

            class FakeRepository:
                db_path = root / "cluster.sqlite3"

                @staticmethod
                def get_worker(_worker_id):
                    return {"status": "online"}

                @staticmethod
                def create_transfer(_worker_id, owner_id="", metadata=None):
                    kind = str((metadata or {}).get("path") or "").split("/", 1)[0]
                    return {"id": f"transfer-{kind}"}

                @staticmethod
                def create_command(data):
                    kind = str(data["payload"]["path"]).split("/", 1)[0]
                    return {"id": f"command-{kind}"}

                @staticmethod
                def get_transfer(transfer_id):
                    kind = transfer_id.removeprefix("transfer-")
                    return {
                        "status": "completed",
                        "relative_path": f"{kind}.zip",
                    }

                @staticmethod
                def get_command(_command_id):
                    return {"status": "completed"}

                @staticmethod
                def update_transfer(*_args, **_kwargs):
                    return None

            cluster = SimpleNamespace(
                config=SimpleNamespace(local_worker_id="ats-worker-controller"),
                repository=FakeRepository(),
                has_command_agent=lambda _worker_id: True,
            )
            report = {
                "report_id": "cluster:job-1:attempt-1",
                "timestamp": "cluster-job-1",
                "report_name": run_folder,
                "source_timestamp": run_folder,
                "suite_path": str(root / "android-gts" / "tools"),
                "worker_id": "worker-remote",
            }
            with patch(
                "features.cluster.get_cluster_service",
                return_value=cluster,
            ):
                bundle = asyncio.run(create_remote_report_bundle(
                    report,
                    owner_id=CURRENT_USER.id,
                    timeout_seconds=1,
                ))

        self.assertIsNotNone(bundle)
        try:
            with zipfile.ZipFile(bundle.path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        f"results/{run_folder}/test_result.xml",
                        f"logs/{run_folder}/host_log.txt",
                    },
                )
        finally:
            remove_report_bundle(bundle.path)

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
                "get_reports": lambda self, limit=50, owner_id=None, include_all=False, **_filters: [
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

    def test_cluster_report_uses_worker_connection_and_tradefed_folder_name(self):
        with TemporaryDirectory() as tmp:
            result_dir = Path(tmp)
            (result_dir / "stdout.log").write_text(
                "RESULT DIRECTORY : "
                "/suite/results/2026.07.30_10.39.50.173_6846\n",
                encoding="utf-8",
            )
            cluster = SimpleNamespace(
                repository=SimpleNamespace(
                    get_worker=lambda worker_id: {
                        "id": worker_id,
                        "address": "172.16.14.233",
                        "capabilities": {"ssh_user": "hcq"},
                    }
                )
            )
            with patch(
                "features.cluster.get_cluster_service",
                return_value=cluster,
            ):
                report = _decorate_report_for_client({
                    "timestamp": "cluster-job-941843984fd44e1b9111532981e188c9",
                    "owner_id": "N387pLbIBhpMw5JsWUL9hg",
                    "display_client_id": "N387pLbIBhpMw5JsWUL9hg",
                    "worker_id": "ats-worker-controller",
                    "result_dir": str(result_dir),
                })

        self.assertEqual(
            report["display_client_id"],
            "hcq@172.16.14.233",
        )
        self.assertEqual(
            report["report_name"],
            "2026.07.30_10.39.50.173_6846",
        )
        self.assertEqual(
            report["source_timestamp"],
            "2026.07.30_10.39.50.173_6846",
        )

    def test_legacy_cluster_start_display_is_replaced_by_report_folder(self):
        report = _decorate_report_for_client({
            "timestamp": "cluster-job-d5568545c51946c58915dab6c110ad29",
            "owner_id": CURRENT_USER.id,
            "report_name": "2026.07.31_18.04.00.815_7887",
            "source_timestamp": "Fri Jul 31 18:04:53 CST 2026",
        })

        self.assertEqual(
            report["source_timestamp"],
            "2026.07.31_18.04.00.815_7887",
        )

    def test_cluster_index_keeps_start_display_separate_from_result_folder(self):
        with TemporaryDirectory() as tmp:
            result_dir = Path(tmp)
            folder_name = "2026.07.31_18.04.00.815_7887"
            (result_dir / "stdout.log").write_text(
                f"RESULT DIRECTORY : /suite/results/{folder_name}\n",
                encoding="utf-8",
            )
            (result_dir / "test_result.xml").write_text(
                '<Result suite_name="GTS" start_display="Fri Jul 31 18:04:53 CST 2026">'
                '<Summary pass="1" failed="0" /></Result>',
                encoding="utf-8",
            )
            saved = []
            fake_db = SimpleNamespace(
                get_report_by_timestamp=lambda *args, **kwargs: None,
                add_report=lambda report: saved.append(report) or True,
            )
            job = {
                "id": "job-1",
                "owner_id": CURRENT_USER.id,
                "suite_key": "GTS:14_r1",
                "suite_path": "/suite/tools",
                "request": {},
                "leases": [],
            }
            artifact = {
                "id": "artifact-1",
                "attempt_id": "attempt-1",
                "filename": "test_result.xml",
                "artifact_type": "report",
            }

            with patch("features.reports.test_report_db", fake_db), patch(
                "features.reports.display.report_client_display_id",
                return_value="hcq",
            ):
                index_cluster_report(job, result_dir, artifact)

        self.assertEqual(saved[0]["report_name"], folder_name)
        self.assertEqual(saved[0]["source_timestamp"], folder_name)
        self.assertEqual(
            saved[0]["start_time"],
            "Fri Jul 31 18:04:53 CST 2026",
        )

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
                "get_reports": lambda self, limit=500, owner_id=None, include_all=False, **_filters: [
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
