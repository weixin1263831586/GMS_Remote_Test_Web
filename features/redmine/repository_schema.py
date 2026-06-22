from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .users import (
    DB_PATH,
    DOCS_DIR,
    _now,
)


class RepositorySchemaMixin:
    def __init__(self, db_path: Path = DB_PATH, docs_dir: Path = DOCS_DIR):
        self.db_path = Path(db_path)
        self.docs_dir = Path(docs_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self, initialize_if_missing: bool = True) -> sqlite3.Connection:
        if initialize_if_missing and not self.db_path.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.docs_dir.mkdir(parents=True, exist_ok=True)
            self.init_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        with self.connect(initialize_if_missing=False) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS redmine_agent_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    assigned_to TEXT,
                    window_start TEXT,
                    window_end TEXT,
                    max_issues INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    issue_count INTEGER DEFAULT 0,
                    processed_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    error TEXT,
                    report_path TEXT,
                    summary_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_issues (
                    issue_id INTEGER PRIMARY KEY,
                    run_id TEXT,
                    subject TEXT,
                    status_name TEXT,
                    priority_name TEXT,
                    project_name TEXT,
                    tracker_name TEXT,
                    author_name TEXT,
                    assigned_to_name TEXT,
                    created_on TEXT,
                    updated_on TEXT,
                    description TEXT,
                    journals_json TEXT DEFAULT '[]',
                    attachments_json TEXT DEFAULT '[]',
                    failures_json TEXT DEFAULT '[]',
                    references_json TEXT DEFAULT '[]',
                    ai_json TEXT DEFAULT '{}',
                    summary TEXT,
                    reply_draft TEXT,
                    doc_path TEXT,
                    doc_content TEXT,
                    analysis_status TEXT DEFAULT 'pending',
                    error TEXT,
                    last_scanned_at TEXT,
                    error_info TEXT DEFAULT '',
                    error_analysis TEXT DEFAULT '',
                    solution TEXT DEFAULT '',
                    patch_direction TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    is_resolved INTEGER DEFAULT 0,
                    scan_count INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    attachment_id TEXT,
                    filename TEXT,
                    content_type TEXT,
                    filesize INTEGER DEFAULT 0,
                    local_path TEXT,
                    analysis_json TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'pending',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_references (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    reference_issue_id INTEGER NOT NULL,
                    score REAL DEFAULT 0,
                    similarity_level TEXT DEFAULT '',
                    reason TEXT,
                    match_details_json TEXT DEFAULT '{}',
                    source TEXT DEFAULT '',
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_issue_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    old_status TEXT DEFAULT '',
                    new_status TEXT DEFAULT '',
                    detected_at TEXT
                );
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS redmine_agent_issue_fts USING fts5(
                        issue_id UNINDEXED,
                        subject,
                        description,
                        summary,
                        failures,
                        doc_content
                    )
                    """
                )
            except sqlite3.OperationalError:
                pass

            # --- safe migrations for columns added after initial schema ---
            self._migrate_columns(conn)
            self._migrate_indexes(conn)

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """Add columns that may not exist in older databases (idempotent)."""
        new_columns = [
            ("redmine_agent_issues", "error_info", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "error_analysis", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "solution", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "patch_direction", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "category", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "is_resolved", "INTEGER DEFAULT 0"),
            ("redmine_agent_issues", "scan_count", "INTEGER DEFAULT 1"),
            ("redmine_agent_issues", "soc_platform", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "android_version", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "fixed_version", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "component", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "start_date", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "due_date", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "closed_on", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "done_ratio", "INTEGER DEFAULT 0"),
            ("redmine_agent_references", "similarity_level", "TEXT DEFAULT ''"),
            ("redmine_agent_references", "match_details_json", "TEXT DEFAULT '{}'"),
            ("redmine_agent_references", "source", "TEXT DEFAULT ''"),
        ]
        for table, column, col_type in new_columns:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            except sqlite3.OperationalError:
                pass  # already exists

    @staticmethod
    def _migrate_indexes(conn: sqlite3.Connection) -> None:
        """Create query-path indexes for Redmine dashboards (idempotent)."""
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_assignee_status
                ON redmine_agent_issues(assigned_to_name, is_resolved, status_name);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_updated
                ON redmine_agent_issues(updated_on, created_on, issue_id);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_resolved_closed
                ON redmine_agent_issues(is_resolved, closed_on, updated_on);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_run
                ON redmine_agent_issues(run_id, priority_name, issue_id);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_runs_started
                ON redmine_agent_runs(started_at, finished_at);
            """
        )

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(self, run_id: str, mode: str, window_start: str, window_end: str, max_issues: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO redmine_agent_runs
                (run_id, status, mode, window_start, window_end, max_issues, started_at)
                VALUES (?, 'running', ?, ?, ?, ?, ?)
                """,
                (run_id, mode, window_start, window_end, max_issues, _now()),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key}=?" for key in fields)
        values = [self._json_value(value) if key.endswith("_json") else value for key, value in fields.items()]
        with self.connect() as conn:
            conn.execute(f"UPDATE redmine_agent_runs SET {columns} WHERE run_id=?", [*values, run_id])

    def mark_stale_running_runs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE redmine_agent_runs
                SET status='interrupted',
                    finished_at=?,
                    error='Process restarted before this scan finished'
                WHERE status='running'
                """,
                (_now(),),
            )
            return cursor.rowcount

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_agent_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM redmine_agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._decode_row(row) if row else None

    def get_latest_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_agent_runs WHERE status='done' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return self._decode_row(row) if row else None

    def list_run_issues(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_agent_issues WHERE run_id=? ORDER BY priority_name, issue_id DESC",
                (run_id,),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------
