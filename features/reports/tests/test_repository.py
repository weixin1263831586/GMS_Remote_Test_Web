from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.reports.repository import TestReportDB as ReportRepository


class ReportRepositoryTests(unittest.TestCase):
    def test_recovers_after_runtime_data_directory_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "reports"
            repository = ReportRepository(str(data_dir / "reports.sqlite3"))

            shutil.rmtree(data_dir)

            self.assertEqual(repository.get_reports(owner_id="alice"), [])

    def test_does_not_import_legacy_json_and_indexes_owner_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "test_reports.json"
            legacy.write_text(
                json.dumps(
                    {
                        "reports": [
                            {
                                "timestamp": "2026.01.01_00.00.00",
                                "owner_id": "user-1",
                                "test_type": "CTS",
                                "cluster_job_id": "job-1",
                                "attempt_id": "attempt-1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            repository = ReportRepository(str(root / "reports/reports.sqlite3"))

            self.assertEqual(repository.get_reports(owner_id="user-1"), [])

            with sqlite3.connect(repository.db_path) as conn:
                indexes = {
                    row[1]
                    for row in conn.execute("PRAGMA index_list('reports')")
                }
            self.assertIn("idx_reports_owner_created", indexes)
            self.assertIn("idx_reports_cluster_job", indexes)

    def test_report_owner_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = ReportRepository(str(Path(tmp) / "reports.sqlite3"))
            with self.assertRaisesRegex(ValueError, "owner_id is required"):
                repository.add_report({"timestamp": "2026.01.01_00.00.00"})

    def test_same_timestamp_is_isolated_by_report_id_and_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = ReportRepository(str(Path(tmp) / "reports.sqlite3"))
            repository.add_report(
                {
                    "report_id": "report-alice",
                    "timestamp": "same-time",
                    "owner_id": "alice-id",
                    "test_type": "CTS",
                }
            )
            repository.add_report(
                {
                    "report_id": "report-bob",
                    "timestamp": "same-time",
                    "owner_id": "bob-id",
                    "test_type": "GTS",
                }
            )

            self.assertEqual(
                [item["report_id"] for item in repository.get_reports(owner_id="alice-id")],
                ["report-alice"],
            )
            self.assertEqual(
                [item["report_id"] for item in repository.get_reports(owner_id="bob-id")],
                ["report-bob"],
            )
            self.assertEqual(
                repository.get_report_by_timestamp(
                    "same-time", owner_id="alice-id"
                )["report_id"],
                "report-alice",
            )
            self.assertEqual(
                repository.get_report_by_timestamp(
                    "same-time", owner_id="bob-id"
                )["report_id"],
                "report-bob",
            )
            self.assertEqual(
                repository.get_report(
                    "report-alice",
                    owner_id="alice-id",
                )["report_id"],
                "report-alice",
            )
            self.assertIsNone(
                repository.get_report(
                    "report-alice",
                    owner_id="bob-id",
                )
            )
            self.assertEqual(
                len(repository.get_reports(include_all=True)),
                2,
            )
            with self.assertRaisesRegex(ValueError, "owner_id is required"):
                repository.get_reports()
            with self.assertRaisesRegex(ValueError, "owner_id is required"):
                repository.get_report_by_timestamp("same-time")
            with self.assertRaisesRegex(ValueError, "owner_id is required"):
                repository.get_report("report-alice")


if __name__ == "__main__":
    unittest.main()
