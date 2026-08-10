#!/usr/bin/env python3
"""Durable report repository with owner and provenance indexes."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from foundation.config import settings


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestReportDB:
    """SQLite-backed report store with mandatory principal ownership."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            self.db_path = str(settings.data_root / "reports" / "reports.sqlite3")
        else:
            requested = Path(db_path)
            if requested.suffix.lower() != ".sqlite3":
                raise ValueError("report database path must use the .sqlite3 suffix")
            self.db_path = str(requested)
        self.lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _open_connection(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = self._open_connection()
        schema_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='reports'"""
        ).fetchone() is not None
        if not schema_exists:
            conn.close()
            self._initialize()
            conn = self._open_connection()
        return conn

    def _initialize(self) -> None:
        with self.lock, self._open_connection() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    test_type TEXT NOT NULL DEFAULT 'UNKNOWN',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    worker_id TEXT NOT NULL DEFAULT '',
                    cluster_job_id TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    automation_run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reports_owner_created
                    ON reports(owner_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_timestamp
                    ON reports(timestamp, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_type_created
                    ON reports(test_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_status_created
                    ON reports(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_cluster_job
                    ON reports(cluster_job_id, attempt_id);
                CREATE INDEX IF NOT EXISTS idx_reports_automation_run
                    ON reports(automation_run_id);
                CREATE INDEX IF NOT EXISTS idx_reports_worker_created
                    ON reports(worker_id, created_at DESC, report_id DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_cluster_created
                    ON reports(cluster_job_id, attempt_id, created_at DESC, report_id DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_attempt_created
                    ON reports(attempt_id, created_at DESC, report_id DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_automation_created
                    ON reports(automation_run_id, created_at DESC, report_id DESC);
                """
            )
            conn.commit()
        Path(self.db_path).chmod(0o600)

    @staticmethod
    def _canonical_report(report_info: dict[str, Any]) -> dict[str, Any]:
        report = dict(report_info or {})
        timestamp = str(report.get("timestamp") or "").strip()
        if not timestamp:
            raise ValueError("report timestamp is required")
        owner_id = str(report.get("owner_id") or "").strip()
        if not owner_id:
            raise ValueError("report owner_id is required")
        report_id = str(report.get("report_id") or "").strip()
        if not report_id:
            report_id = f"report-{uuid.uuid4().hex}"
        report["report_id"] = report_id
        report["timestamp"] = timestamp
        report["owner_id"] = owner_id
        report.pop("client_id", None)
        created_at = str(report.get("created_at") or _now())
        report["created_at"] = created_at
        report["updated_at"] = str(report.get("updated_at") or created_at)
        return report

    def _upsert(self, conn: sqlite3.Connection, report_info: dict[str, Any]) -> dict[str, Any]:
        report = self._canonical_report(report_info)
        report["updated_at"] = _now()
        conn.execute(
            """
            INSERT INTO reports (
                report_id, timestamp, owner_id, test_type, status, worker_id,
                cluster_job_id, attempt_id, automation_run_id,
                created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_id) DO UPDATE SET
                timestamp=excluded.timestamp,
                owner_id=excluded.owner_id,
                test_type=excluded.test_type,
                status=excluded.status,
                worker_id=excluded.worker_id,
                cluster_job_id=excluded.cluster_job_id,
                attempt_id=excluded.attempt_id,
                automation_run_id=excluded.automation_run_id,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (
                report["report_id"],
                report["timestamp"],
                report["owner_id"],
                str(report.get("test_type") or "UNKNOWN"),
                str(report.get("status") or "unknown"),
                str(report.get("worker_id") or ""),
                str(report.get("cluster_job_id") or ""),
                str(report.get("attempt_id") or ""),
                str(report.get("automation_run_id") or ""),
                report["created_at"],
                report["updated_at"],
                json.dumps(report, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return report

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        payload["report_id"] = str(row["report_id"])
        payload["owner_id"] = str(row["owner_id"])
        payload["created_at"] = str(row["created_at"])
        payload["updated_at"] = str(row["updated_at"])
        payload.pop("client_id", None)
        return payload

    def add_report(self, report_info: dict[str, Any]) -> bool:
        with self.lock, self._connect() as conn:
            report = dict(report_info or {})
            if not report.get("report_id") and report.get("timestamp"):
                existing = conn.execute(
                    """
                    SELECT report_id, created_at FROM reports
                    WHERE timestamp=? AND owner_id=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (
                        str(report.get("timestamp")),
                        str(report.get("owner_id") or ""),
                    ),
                ).fetchone()
                if existing:
                    report["report_id"] = str(existing["report_id"])
                    report.setdefault("created_at", str(existing["created_at"]))
            stored = self._upsert(conn, report)
            conn.commit()
        logger.info("Stored report %s", stored["report_id"])
        return True

    def get_reports(
        self,
        limit: int = 50,
        test_type: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        include_all: bool = False,
        worker_id: str | None = None,
        cluster_job_id: str | None = None,
        attempt_id: str | None = None,
        automation_run_id: str | None = None,
        before_created_at: str | None = None,
        before_report_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not owner_id and not include_all:
            raise ValueError("owner_id is required unless include_all is explicit")
        filters: list[str] = []
        values: list[Any] = []
        if owner_id:
            filters.append("owner_id=?")
            values.append(owner_id)
        if test_type:
            filters.append("test_type=?")
            values.append(test_type)
        if status:
            filters.append("status=?")
            values.append(status)
        for column, value in (
            ("worker_id", worker_id),
            ("cluster_job_id", cluster_job_id),
            ("attempt_id", attempt_id),
            ("automation_run_id", automation_run_id),
        ):
            if value:
                filters.append(f"{column}=?")
                values.append(value)
        if before_created_at:
            if before_report_id:
                filters.append(
                    "(created_at < ? OR (created_at = ? AND report_id < ?))"
                )
                values.extend(
                    [before_created_at, before_created_at, before_report_id]
                )
            else:
                filters.append("created_at < ?")
                values.append(before_created_at)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(max(1, min(int(limit), 5000)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM reports {where} "
                "ORDER BY created_at DESC, report_id DESC LIMIT ?",
                values,
            ).fetchall()
        return [item for row in rows if (item := self._decode(row)) is not None]

    def get_report_by_timestamp(
        self,
        timestamp: str,
        *,
        owner_id: str | None = None,
        include_all: bool = False,
    ) -> dict[str, Any] | None:
        if not owner_id and not include_all:
            raise ValueError("owner_id is required unless include_all is explicit")
        where = "timestamp=?"
        values: list[Any] = [timestamp]
        if owner_id:
            where += " AND owner_id=?"
            values.append(owner_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM reports WHERE {where} ORDER BY created_at DESC LIMIT 1",
                values,
            ).fetchone()
        return self._decode(row)

    def get_report(
        self,
        report_id: str,
        *,
        owner_id: str | None = None,
        include_all: bool = False,
    ) -> dict[str, Any] | None:
        if not owner_id and not include_all:
            raise ValueError("owner_id is required unless include_all is explicit")
        where = "report_id=?"
        values: list[Any] = [report_id]
        if owner_id:
            where += " AND owner_id=?"
            values.append(owner_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM reports WHERE {where}",
                values,
            ).fetchone()
        return self._decode(row)

    def update_report_status(
        self,
        timestamp: str,
        status: str,
        *,
        owner_id: str,
        **kwargs: Any,
    ) -> bool:
        report = self.get_report_by_timestamp(timestamp, owner_id=owner_id)
        if report is None:
            return False
        report.update(kwargs)
        report["status"] = status
        return self.add_report(report)

    def delete_report(self, timestamp: str, *, owner_id: str) -> bool:
        report = self.get_report_by_timestamp(timestamp, owner_id=owner_id)
        if report is None:
            return False
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM reports WHERE report_id=?",
                (report["report_id"],),
            )
            conn.commit()
        return cursor.rowcount == 1

    def delete_report_by_id(
        self,
        report_id: str,
        *,
        owner_id: str | None = None,
        include_all: bool = False,
    ) -> bool:
        if not owner_id and not include_all:
            raise ValueError("owner_id is required unless include_all is explicit")
        where = "report_id=?"
        values: list[Any] = [report_id]
        if owner_id:
            where += " AND owner_id=?"
            values.append(owner_id)
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM reports WHERE {where}",
                values,
            )
            conn.commit()
        return cursor.rowcount == 1

    def get_statistics(self, *, owner_id: str) -> dict[str, Any]:
        if not owner_id:
            raise ValueError("owner_id is required")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        owner_where = " WHERE owner_id=?" if owner_id else ""
        owner_and = " AND owner_id=?" if owner_id else ""
        owner_values: tuple[Any, ...] = (owner_id,) if owner_id else ()
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM reports{owner_where}", owner_values
                ).fetchone()[0]
            )
            types = conn.execute(
                f"SELECT test_type, COUNT(*) AS count FROM reports{owner_where} GROUP BY test_type",
                owner_values,
            ).fetchall()
            recent = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM reports WHERE created_at>=?{owner_and}",
                    (week_ago, *owner_values),
                ).fetchone()[0]
            )
            last = conn.execute(
                f"SELECT MAX(updated_at) FROM reports{owner_where}", owner_values
            ).fetchone()[0]
        return {
            "total_reports": total,
            "type_counts": {str(row["test_type"]): int(row["count"]) for row in types},
            "recent_week": recent,
            "last_update": last,
        }

test_report_db = TestReportDB()
