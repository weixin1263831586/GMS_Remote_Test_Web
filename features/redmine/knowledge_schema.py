"""Schema for the internal Redmine knowledge base.

The knowledge base is a *separate* sqlite database from the issue scan store
(``redmine.sqlite3``). It holds structured "case facts", aggregated "mature
cases", optional reference outputs (GMS assistant / manual), case evaluations
and internal-issue links. It is intentionally self-contained: ``case_facts``
mirrors the issue fields it needs so searches do not require a cross-database
join against the scan store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class KnowledgeSchemaMixin:
    """Creates the knowledge base tables (idempotent)."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS redmine_case_facts (
                    issue_id INTEGER PRIMARY KEY,
                    subject TEXT DEFAULT '',
                    status_name TEXT DEFAULT '',
                    assigned_to_name TEXT DEFAULT '',
                    project_name TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    chip_platform TEXT DEFAULT '',
                    android_version TEXT DEFAULT '',
                    certification_type TEXT DEFAULT '',
                    module TEXT DEFAULT '',
                    product_form TEXT DEFAULT '',
                    region TEXT DEFAULT '',
                    error_signature TEXT DEFAULT '',
                    problem_summary TEXT DEFAULT '',
                    symptoms_json TEXT DEFAULT '[]',
                    root_cause TEXT DEFAULT '',
                    solution TEXT DEFAULT '',
                    verification TEXT DEFAULT '',
                    reply_template TEXT DEFAULT '',
                    keywords_json TEXT DEFAULT '[]',
                    evidence_json TEXT DEFAULT '{}',
                    doc_excerpt TEXT DEFAULT '',
                    confidence REAL DEFAULT 0,
                    source_quality TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_mature_cases (
                    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'draft',
                    canonical_error_signature TEXT DEFAULT '',
                    chip_platform TEXT DEFAULT '',
                    android_version TEXT DEFAULT '',
                    certification_type TEXT DEFAULT '',
                    module TEXT DEFAULT '',
                    product_form TEXT DEFAULT '',
                    region TEXT DEFAULT '',
                    problem_summary TEXT DEFAULT '',
                    scope_json TEXT DEFAULT '{}',
                    symptoms_json TEXT DEFAULT '[]',
                    root_cause TEXT DEFAULT '',
                    solution_json TEXT DEFAULT '{}',
                    notes_json TEXT DEFAULT '[]',
                    rules_json TEXT DEFAULT '[]',
                    reply_template TEXT DEFAULT '',
                    source_issue_ids_json TEXT DEFAULT '[]',
                    evidence_json TEXT DEFAULT '{}',
                    keywords_json TEXT DEFAULT '[]',
                    confidence REAL DEFAULT 0,
                    approved_by TEXT DEFAULT '',
                    approved_at TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_case_issue_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL,
                    issue_id INTEGER NOT NULL,
                    score REAL DEFAULT 0,
                    reason TEXT DEFAULT '',
                    evidence_json TEXT DEFAULT '{}',
                    created_at TEXT,
                    UNIQUE(case_id, issue_id)
                );

                CREATE TABLE IF NOT EXISTS redmine_reference_outputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    markdown TEXT DEFAULT '',
                    structured_json TEXT DEFAULT '{}',
                    raw_output TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_case_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    internal_case_json TEXT DEFAULT '{}',
                    reference_case_json TEXT DEFAULT '{}',
                    score REAL DEFAULT 0,
                    missing_fields_json TEXT DEFAULT '[]',
                    mismatch_fields_json TEXT DEFAULT '[]',
                    suggestions_json TEXT DEFAULT '[]',
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_internal_issue_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_issue_id INTEGER,
                    case_id INTEGER,
                    internal_issue_id INTEGER NOT NULL,
                    created_by TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}',
                    created_at TEXT
                );
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS redmine_case_facts_fts USING fts5(
                        issue_id UNINDEXED,
                        subject,
                        problem_summary,
                        error_signature,
                        root_cause,
                        solution,
                        keywords
                    )
                    """
                )
            except sqlite3.OperationalError:
                pass

            self._migrate_indexes(conn)

    @staticmethod
    def _migrate_indexes(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_case_facts_module
                ON redmine_case_facts(module, chip_platform);
            CREATE INDEX IF NOT EXISTS idx_case_facts_signature
                ON redmine_case_facts(error_signature);
            CREATE INDEX IF NOT EXISTS idx_mature_cases_status
                ON redmine_mature_cases(status, module);
            CREATE INDEX IF NOT EXISTS idx_case_issue_links_case
                ON redmine_case_issue_links(case_id);
            CREATE INDEX IF NOT EXISTS idx_reference_outputs_issue
                ON redmine_reference_outputs(issue_id, source);
            """
        )

    def reset(self) -> None:
        """Delete all knowledge base data but keep the empty database shell."""
        with self.connect() as conn:
            conn.executescript(
                """
                DELETE FROM redmine_internal_issue_links;
                DELETE FROM redmine_case_evaluations;
                DELETE FROM redmine_reference_outputs;
                DELETE FROM redmine_case_issue_links;
                DELETE FROM redmine_mature_cases;
                DELETE FROM redmine_case_facts;
                """
            )
            try:
                conn.execute("DELETE FROM redmine_case_facts_fts")
            except sqlite3.OperationalError:
                pass

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _json_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _decode_row(row: sqlite3.Row, *, default_obj: bool = False) -> dict[str, Any]:
        item = dict(row)
        for key, val in list(item.items()):
            if not key.endswith("_json"):
                continue
            try:
                item[key] = json.loads(val) if val else ({} if default_obj else [])
            except Exception:
                item[key] = {}
        return item

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = [token for token in query.replace('"', " ").split() if len(token) >= 2][:12]
        return " OR ".join(f'"{token}"' for token in tokens) or '"empty"'
