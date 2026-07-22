from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.reports import files_api


class FakeReportDB:
    def __init__(self, reports: list[dict]):
        self.reports = reports

    def get_reports(self, limit=50, owner_id=None, include_all=False, **_filters):
        return [
            report for report in self.reports
            if include_all or report.get("owner_id") == owner_id
        ][:limit]

    def get_report_by_timestamp(
        self,
        timestamp,
        *,
        owner_id=None,
        include_all=False,
    ):
        return next(
            (
                report for report in self.reports
                if report["timestamp"] == timestamp
                and (include_all or report.get("owner_id") == owner_id)
            ),
            None,
        )


class ReportAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.alice_dir = root / "alice" / "results" / "alice-report"
        self.bob_dir = root / "bob" / "results" / "bob-report"
        self.alice_dir.mkdir(parents=True)
        self.bob_dir.mkdir(parents=True)
        (self.alice_dir / "test_result.xml").write_text("alice", encoding="utf-8")
        (self.bob_dir / "test_result.xml").write_text("bob", encoding="utf-8")
        self.database = FakeReportDB(
            [
                {
                    "timestamp": "alice-report",
                    "owner_id": "id-alice",
                    "result_dir": str(self.alice_dir),
                },
                {
                    "timestamp": "bob-report",
                    "owner_id": "id-bob",
                    "result_dir": str(self.bob_dir),
                },
            ],
        )
        app = FastAPI()

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            username = request.headers.get("X-Test-User", "alice")
            role = request.headers.get("X-Test-Role", "user")
            request.state.current_user = CurrentUser(
                id=f"id-{username}",
                username=username,
                role=role,
            )
            return await call_next(request)

        app.include_router(files_api.router)
        self.client = TestClient(app)
        self.original_file_utils = files_api.dependencies.file_utils
        files_api.dependencies.file_utils = object()
        self.database_patch = patch.object(files_api, "test_report_db", self.database)
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        files_api.dependencies.file_utils = self.original_file_utils
        self.client.close()
        self.tmp.cleanup()

    def test_regular_user_lists_only_owned_reports_and_admin_lists_all(self):
        alice = self.client.get("/api/reports/list")
        admin = self.client.get(
            "/api/reports/list",
            headers={"X-Test-User": "admin", "X-Test-Role": "admin"},
        )

        self.assertEqual(
            [report["timestamp"] for report in alice.json()["reports"]],
            ["alice-report"],
        )
        self.assertEqual(
            {report["timestamp"] for report in admin.json()["reports"]},
            {"alice-report", "bob-report"},
        )

    def test_other_users_report_and_file_path_are_hidden(self):
        report = self.client.get(
            "/api/reports/download",
            params={"report_timestamp": "bob-report"},
        )
        report_file = self.client.get(
            "/api/reports/download",
            params={"report_timestamp": "bob-report", "file": "test_result.xml"},
        )

        self.assertEqual(report.status_code, 404)
        self.assertEqual(report_file.status_code, 404)

    def test_absolute_report_path_contract_is_removed(self):
        response = self.client.get(
            "/api/reports/download",
            params={"path": str(self.alice_dir / "test_result.xml")},
        )
        self.assertEqual(response.status_code, 410)

    def test_router_rejects_anonymous_access_without_global_middleware(self):
        app = FastAPI()
        app.include_router(files_api.router)
        with patch.dict(
            os.environ, {"GMS_ENV": "production", "GMS_AUTH_REQUIRED": "true"}
        ), TestClient(app) as anonymous:
            response = anonymous.get("/api/reports/list")
        self.assertEqual(response.status_code, 401)

    def test_symlink_cannot_escape_registered_report_directory(self):
        outside = Path(self.tmp.name) / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.alice_dir / "linked-secret.txt"
        link.symlink_to(outside)

        request = type(
            "RequestStub",
            (),
            {"state": type("State", (), {"current_user": CurrentUser(
                id="id-alice", username="alice", role="user"
            )})()},
        )()
        self.assertFalse(
            files_api._is_registered_report_file_path(str(link), request)
        )


if __name__ == "__main__":
    unittest.main()
