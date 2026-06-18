#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import argparse
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1


from .fetching import *
from .models import *
from .repository import *


def select_sources(source_keys: list[str] | None) -> list[SourceConfig]:
    if not source_keys:
        return list(SOURCES)
    known = {source.key: source for source in SOURCES}
    unknown = sorted(set(source_keys) - set(known))
    if unknown:
        raise ValueError(f'unknown source key(s): {", ".join(unknown)}')
    return [known[key] for key in source_keys]


def create_scan_run(conn: sqlite3.Connection, mode: str, source_filter: str, started_at: str) -> int:
    conn.execute(
        """
        INSERT INTO gms_update_scan_runs (
            mode, source_filter, started_at, finished_at, sources_scanned, sources_skipped,
            artifacts_total, packages_total, requirement_sections_total, requirement_table_rows_total,
            changes_total, success, error
        )
        VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, '')
        """,
        (mode, source_filter, started_at, started_at),
    )
    return int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])


def finish_scan_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    sources_scanned: int,
    sources_skipped: int,
    artifacts_total: int,
    packages_total: int,
    requirement_sections_total: int,
    requirement_table_rows_total: int,
    changes_total: int,
    success: bool,
    error: str = '',
) -> None:
    conn.execute(
        """
        UPDATE gms_update_scan_runs
        SET finished_at = ?, sources_scanned = ?, sources_skipped = ?,
            artifacts_total = ?, packages_total = ?, requirement_sections_total = ?,
            requirement_table_rows_total = ?, changes_total = ?, success = ?, error = ?
        WHERE id = ?
        """,
        (
            finished_at,
            sources_scanned,
            sources_skipped,
            artifacts_total,
            packages_total,
            requirement_sections_total,
            requirement_table_rows_total,
            changes_total,
            int(success),
            error,
            run_id,
        ),
    )


def run_sync(args: argparse.Namespace) -> int:
    selected_sources = select_sources(args.source)
    force = args.mode == 'full'
    started_at = utc_now()
    session = build_session(args)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        source_filter = ','.join(source.key for source in selected_sources)
        run_id = create_scan_run(conn, args.mode, source_filter, started_at)
        conn.commit()

        sources_scanned = 0
        sources_skipped = 0
        artifacts_total = 0
        packages_total = 0
        requirement_sections_total = 0
        requirement_table_rows_total = 0
        changes_total = 0
        try:
            for source in selected_sources:
                if args.verbose:
                    print(f'fetching {source.key}: {source.url}', file=sys.stderr)
                fetched = fetch_source(session, source, args.timeout)
                scanned, changes, parsed = sync_source(conn, run_id, fetched, force=force, timestamp=utc_now())
                if scanned:
                    sources_scanned += 1
                else:
                    sources_skipped += 1
                artifacts_total += len(parsed.artifacts)
                packages_total += len(parsed.gms_packages)
                requirement_sections_total += len(parsed.requirement_sections)
                requirement_table_rows_total += len(parsed.requirement_table_rows)
                changes_total += changes
                if args.verbose:
                    state = 'parsed' if scanned else 'unchanged'
                    print(
                        f'{source.key}: {state}, changes={changes}, artifacts={len(parsed.artifacts)}, '
                        f'packages={len(parsed.gms_packages)}, req_sections={len(parsed.requirement_sections)}, '
                        f'req_rows={len(parsed.requirement_table_rows)}',
                        file=sys.stderr,
                    )
            finish_scan_run(
                conn,
                run_id,
                finished_at=utc_now(),
                sources_scanned=sources_scanned,
                sources_skipped=sources_skipped,
                artifacts_total=artifacts_total,
                packages_total=packages_total,
                requirement_sections_total=requirement_sections_total,
                requirement_table_rows_total=requirement_table_rows_total,
                changes_total=changes_total,
                success=True,
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            init_db(conn)
            finish_scan_run(
                conn,
                run_id,
                finished_at=utc_now(),
                sources_scanned=sources_scanned,
                sources_skipped=sources_skipped,
                artifacts_total=artifacts_total,
                packages_total=packages_total,
                requirement_sections_total=requirement_sections_total,
                requirement_table_rows_total=requirement_table_rows_total,
                changes_total=changes_total,
                success=False,
                error=str(exc),
            )
            conn.commit()
            print(f'error: {exc}', file=sys.stderr)
            return 1

    print(
        f'run_id={run_id} mode={args.mode} sources_scanned={sources_scanned} sources_skipped={sources_skipped} '
        f'artifacts={artifacts_total} packages={packages_total} requirement_sections={requirement_sections_total} '
        f'requirement_table_rows={requirement_table_rows_total} changes={changes_total} db={args.db}'
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Scan CTS/GMS update pages and store structured change records.')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB_PATH, help=f'Default: {DEFAULT_DB_PATH}')
    parser.add_argument('--mode', choices=('incremental', 'full'), default='incremental')
    parser.add_argument('--source', action='append', choices=[source.key for source in SOURCES], help='Source key to scan. Repeatable.')
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--browser', choices=('auto', 'firefox', 'chromium'), default='auto')
    parser.add_argument('--cookie-file', type=Path)
    parser.add_argument('--cookie-header')
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def main() -> int:
    return run_sync(build_parser().parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
