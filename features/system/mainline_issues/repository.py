#!/usr/bin/env python3
"""Sync Mainline known issues from Android Partner release notes."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import re
import sqlite3
from pathlib import Path


DEFAULT_INDEX_URL = 'https://docs.partner.android.com/mainline/release/release-notes?authuser=2'
DEFAULT_DB_PATH = Path('data/mainline_known_issues.sqlite3')
KNOWN_ISSUE_HEADING_RE = re.compile(r'^(MTS|CTS|GTS)\s+known issues\b.*:$', flags=re.IGNORECASE)
PRODUCT_SECTIONS = ('Android', 'Android Go')



from .parser import *


def _migrate_sync_runs_table(conn: sqlite3.Connection) -> None:
    """迁移 sync_runs 表：确保 release_year 列允许 NULL。"""
    col_info = conn.execute("PRAGMA table_info('mainline_known_issue_sync_runs')").fetchall()
    if not col_info:
        return  # 表不存在，CREATE TABLE IF NOT EXISTS 会处理
    for col in col_info:
        if col[1] == 'release_year' and col[3]:  # col[3] = notnull
            conn.execute('ALTER TABLE mainline_known_issue_sync_runs RENAME TO mainline_known_issue_sync_runs_old')
            conn.execute(
                """
                CREATE TABLE mainline_known_issue_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    index_url TEXT NOT NULL,
                    release_year INTEGER,
                    pages_scanned INTEGER NOT NULL,
                    pages_skipped INTEGER NOT NULL DEFAULT 0,
                    issues_found INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO mainline_known_issue_sync_runs (
                    id, index_url, release_year, pages_scanned, pages_skipped,
                    issues_found, started_at, finished_at
                )
                SELECT id, index_url, release_year, pages_scanned, pages_skipped,
                       issues_found, started_at, finished_at
                FROM mainline_known_issue_sync_runs_old
                """
            )
            conn.execute('DROP TABLE mainline_known_issue_sync_runs_old')
            break


def init_db(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info('mainline_known_issues')").fetchall()
    }
    if existing_columns and 'issue_type' not in existing_columns:
        conn.execute('ALTER TABLE mainline_known_issues RENAME TO mainline_known_issues_old')
    elif existing_columns:
        indexes = conn.execute("PRAGMA index_list('mainline_known_issues')").fetchall()
        has_global_unique = False
        for index in indexes:
            if not index[2]:
                continue
            columns = [
                row[2]
                for row in conn.execute(f"PRAGMA index_info('{index[1]}')").fetchall()
            ]
            if columns == ['product_section', 'issue_type', 'test_module', 'test_case', 'exemption_id']:
                has_global_unique = True
                break
        if not has_global_unique:
            conn.execute('ALTER TABLE mainline_known_issues RENAME TO mainline_known_issues_old')

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mainline_known_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            source_title TEXT NOT NULL,
            release_year INTEGER NOT NULL,
            release_label TEXT NOT NULL,
            product_section TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            android_versions TEXT NOT NULL,
            category TEXT NOT NULL,
            test_module TEXT NOT NULL,
            test_case TEXT NOT NULL,
            exemption_id TEXT NOT NULL,
            issue_text TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(product_section, issue_type, test_module, test_case, exemption_id)
        )
        """
    )
    old_table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mainline_known_issues_old'"
    ).fetchone()
    if old_table_exists and existing_columns and 'issue_type' not in existing_columns:
        conn.execute(
            """
            INSERT OR IGNORE INTO mainline_known_issues (
                source_url, source_title, release_year, release_label, product_section, issue_type,
                android_versions, category, test_module, test_case, exemption_id,
                issue_text, first_seen_at, last_seen_at
            )
            SELECT
                source_url, source_title, release_year, release_label, product_section, 'CTS',
                android_versions, category, test_module, test_case, exemption_id,
                issue_text, first_seen_at, last_seen_at
            FROM mainline_known_issues_old
            """
        )
        conn.execute('DROP TABLE mainline_known_issues_old')
    elif old_table_exists:
        conn.execute(
            """
            INSERT OR IGNORE INTO mainline_known_issues (
                source_url, source_title, release_year, release_label, product_section, issue_type,
                android_versions, category, test_module, test_case, exemption_id,
                issue_text, first_seen_at, last_seen_at
            )
            SELECT
                source_url, source_title, release_year, release_label, product_section, issue_type,
                android_versions, category, test_module, test_case, exemption_id,
                issue_text, first_seen_at, last_seen_at
            FROM mainline_known_issues_old
            ORDER BY id
            """
        )
        conn.execute('DROP TABLE mainline_known_issues_old')
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mainline_known_issues_lookup
        ON mainline_known_issues(issue_type, test_module, test_case, exemption_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mainline_known_issue_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_url TEXT NOT NULL,
            release_year INTEGER,
            pages_scanned INTEGER NOT NULL,
            pages_skipped INTEGER NOT NULL DEFAULT 0,
            issues_found INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        )
        """
    )
    sync_run_columns = {
        row[1] for row in conn.execute("PRAGMA table_info('mainline_known_issue_sync_runs')").fetchall()
    }
    if 'pages_skipped' not in sync_run_columns:
        conn.execute('ALTER TABLE mainline_known_issue_sync_runs ADD COLUMN pages_skipped INTEGER NOT NULL DEFAULT 0')
    # 迁移：旧表中 release_year 可能有 NOT NULL 约束，需重建为可空
    _migrate_sync_runs_table(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mainline_release_note_pages (
            source_url TEXT PRIMARY KEY,
            release_year INTEGER NOT NULL,
            release_label TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            final_url TEXT NOT NULL,
            issues_found INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL
        )
        """
    )


def get_page_state(conn: sqlite3.Connection, source_url: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT content_hash, issues_found, first_seen_at, last_scanned_at, last_changed_at
        FROM mainline_release_note_pages
        WHERE source_url = ?
        """,
        (source_url,),
    ).fetchone()


def replace_page_issues(conn: sqlite3.Connection, source_url: str, issues: list[KnownIssue], timestamp: str) -> None:
    conn.execute('DELETE FROM mainline_known_issues WHERE source_url = ?', (source_url,))
    upsert_issues(conn, issues, timestamp)


def load_known_issue_keys(
    conn: sqlite3.Connection,
    *,
    exclude_source_url: str | None = None,
) -> set[tuple[str, str, str, str, str]]:
    where = 'WHERE source_url != ?' if exclude_source_url else ''
    params = (exclude_source_url,) if exclude_source_url else ()
    return {
        (row['product_section'], row['issue_type'], row['test_module'], row['test_case'], row['exemption_id'])
        for row in conn.execute(
            f"""
            SELECT product_section, issue_type, test_module, test_case, exemption_id
            FROM mainline_known_issues
            {where}
            """,
            params,
        )
    }


def upsert_page_state(
    conn: sqlite3.Connection,
    page: ReleasePage,
    fetched: FetchedPage,
    issues_found: int,
    timestamp: str,
    *,
    changed: bool,
) -> None:
    existing = get_page_state(conn, page.url)
    first_seen_at = existing['first_seen_at'] if existing else timestamp
    last_changed_at = timestamp if changed or not existing else existing['last_changed_at']
    conn.execute(
        """
        INSERT INTO mainline_release_note_pages (
            source_url, release_year, release_label, content_hash, status_code, final_url,
            issues_found, first_seen_at, last_scanned_at, last_changed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_url)
        DO UPDATE SET
            release_year = excluded.release_year,
            release_label = excluded.release_label,
            content_hash = excluded.content_hash,
            status_code = excluded.status_code,
            final_url = excluded.final_url,
            issues_found = excluded.issues_found,
            last_scanned_at = excluded.last_scanned_at,
            last_changed_at = excluded.last_changed_at
        """,
        (
            page.url,
            page.year,
            page.label,
            fetched.content_hash,
            fetched.status_code,
            fetched.final_url,
            issues_found,
            first_seen_at,
            timestamp,
            last_changed_at,
        ),
    )


def upsert_issues(conn: sqlite3.Connection, issues: list[KnownIssue], timestamp: str) -> None:
    conn.executemany(
        """
        INSERT INTO mainline_known_issues (
            source_url, source_title, release_year, release_label, product_section, issue_type,
            android_versions, category, test_module, test_case, exemption_id,
            issue_text, first_seen_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_section, issue_type, test_module, test_case, exemption_id)
        DO UPDATE SET
            android_versions = excluded.android_versions,
            category = excluded.category,
            issue_text = excluded.issue_text,
            last_seen_at = excluded.last_seen_at
        """,
        [
            (
                issue.source_url,
                issue.source_title,
                issue.release_year,
                issue.release_label,
                issue.product_section,
                issue.issue_type,
                issue.android_versions,
                issue.category,
                issue.test_module,
                issue.test_case,
                issue.exemption_id,
                issue.issue_text,
                timestamp,
                timestamp,
            )
            for issue in issues
        ],
    )
