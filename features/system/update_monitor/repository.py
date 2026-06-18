#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1


from .models import *
from .parsers import *


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            source_filter TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            sources_scanned INTEGER NOT NULL,
            sources_skipped INTEGER NOT NULL,
            artifacts_total INTEGER NOT NULL,
            packages_total INTEGER NOT NULL,
            requirement_sections_total INTEGER NOT NULL,
            requirement_table_rows_total INTEGER NOT NULL,
            changes_total INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_sources (
            source_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            final_url TEXT NOT NULL,
            category TEXT NOT NULL,
            parser TEXT NOT NULL,
            auth_required INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            title TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_artifacts (
            source_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            suite_type TEXT NOT NULL,
            android_version TEXT NOT NULL,
            release_name TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            arch TEXT NOT NULL,
            file_name TEXT NOT NULL,
            download_url TEXT NOT NULL,
            release_notes_url TEXT NOT NULL,
            user_guide_url TEXT NOT NULL,
            ci_build_id TEXT NOT NULL,
            target_platform TEXT NOT NULL,
            description TEXT NOT NULL,
            section_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, item_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_packages (
            source_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            section TEXT NOT NULL,
            android_version TEXT NOT NULL,
            release_notes_url TEXT NOT NULL,
            file_name TEXT NOT NULL,
            download_url TEXT NOT NULL,
            required_from TEXT NOT NULL,
            partner_gerrit_tag TEXT NOT NULL,
            partner_gerrit_url TEXT NOT NULL,
            description TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, item_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_requirement_sections (
            source_key TEXT NOT NULL,
            section_key TEXT NOT NULL,
            level INTEGER NOT NULL,
            number TEXT NOT NULL,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            text_excerpt TEXT NOT NULL,
            table_count INTEGER NOT NULL,
            link_count INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, section_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_requirement_table_rows (
            source_key TEXT NOT NULL,
            row_key TEXT NOT NULL,
            section_key TEXT NOT NULL,
            section_title TEXT NOT NULL,
            table_index INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            headers_json TEXT NOT NULL,
            values_json TEXT NOT NULL,
            row_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, row_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_requirement_version_tags (
            source_key TEXT NOT NULL,
            tag_key TEXT NOT NULL,
            android_version TEXT NOT NULL,
            change_kind TEXT NOT NULL,
            section_key TEXT NOT NULL,
            section_title TEXT NOT NULL,
            requirement_ids TEXT NOT NULL,
            text_excerpt TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, tag_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_change_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            change_type TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            detected_at TEXT NOT NULL
        )
        """
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_artifacts_suite ON gms_update_artifacts(suite_type, android_version)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_packages_version ON gms_update_packages(android_version, section)')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_gms_update_requirement_sections_path '
        'ON gms_update_requirement_sections(level, number, title)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_gms_update_requirement_version_tags_lookup '
        'ON gms_update_requirement_version_tags(android_version, change_kind, section_title)'
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_changes_run ON gms_update_change_events(run_id, source_key)')


def dataclass_public_json(record: Any, *, exclude: tuple[str, ...] = ()) -> str:
    data = asdict(record)
    for key in exclude:
        data.pop(key, None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def row_public_json(row: sqlite3.Row, *, skip_time: bool = True) -> str:
    data = dict(row)
    if skip_time:
        data.pop('first_seen_at', None)
        data.pop('last_seen_at', None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def record_change(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    source_key: str,
    entity_type: str,
    entity_key: str,
    change_type: str,
    before_json: str,
    after_json: str,
    detected_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO gms_update_change_events (
            run_id, source_key, entity_type, entity_key, change_type,
            before_json, after_json, detected_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, source_key, entity_type, entity_key, change_type, before_json, after_json, detected_at),
    )


def replace_records(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    source_key: str,
    table: str,
    key_column: str,
    records: list[Any],
    columns: list[str],
    entity_type: str,
    timestamp: str,
) -> int:
    existing = {
        row[key_column]: row
        for row in conn.execute(f'SELECT * FROM {table} WHERE source_key = ?', (source_key,)).fetchall()
    }
    incoming = {getattr(record, key_column): record for record in records}
    changes = 0

    for key, row in existing.items():
        if key in incoming:
            continue
        changes += 1
        record_change(
            conn,
            run_id=run_id,
            source_key=source_key,
            entity_type=entity_type,
            entity_key=key,
            change_type='removed',
            before_json=row_public_json(row),
            after_json='',
            detected_at=timestamp,
        )
        conn.execute(f'DELETE FROM {table} WHERE source_key = ? AND {key_column} = ?', (source_key, key))

    insert_columns = [*columns, 'first_seen_at', 'last_seen_at']
    placeholders = ', '.join(['?'] * len(insert_columns))
    update_assignments = ', '.join([f'{column} = excluded.{column}' for column in columns if column not in ('source_key', key_column)])
    update_assignments = f'{update_assignments}, last_seen_at = excluded.last_seen_at'

    for key, record in incoming.items():
        before = existing.get(key)
        after_json = dataclass_public_json(record)
        if before is None:
            changes += 1
            record_change(
                conn,
                run_id=run_id,
                source_key=source_key,
                entity_type=entity_type,
                entity_key=key,
                change_type='added',
                before_json='',
                after_json=after_json,
                detected_at=timestamp,
            )
            first_seen_at = timestamp
        else:
            first_seen_at = before['first_seen_at']
            if before['content_hash'] != record.content_hash:
                changes += 1
                record_change(
                    conn,
                    run_id=run_id,
                    source_key=source_key,
                    entity_type=entity_type,
                    entity_key=key,
                    change_type='changed',
                    before_json=row_public_json(before),
                    after_json=after_json,
                    detected_at=timestamp,
                )
        values = [getattr(record, column) for column in columns]
        values.extend([first_seen_at, timestamp])
        conn.execute(
            f"""
            INSERT INTO {table} ({', '.join(insert_columns)})
            VALUES ({placeholders})
            ON CONFLICT(source_key, {key_column}) DO UPDATE SET {update_assignments}
            """,
            values,
        )
    return changes


def upsert_source(conn: sqlite3.Connection, fetched: FetchedDocument, timestamp: str) -> bool:
    existing = conn.execute(
        'SELECT content_hash, first_seen_at, last_changed_at FROM gms_update_sources WHERE source_key = ?',
        (fetched.source.key,),
    ).fetchone()
    changed = not existing or existing['content_hash'] != fetched.content_hash
    first_seen_at = existing['first_seen_at'] if existing else timestamp
    last_changed_at = timestamp if changed else existing['last_changed_at']
    conn.execute(
        """
        INSERT INTO gms_update_sources (
            source_key, name, url, final_url, category, parser, auth_required,
            content_hash, status_code, title, first_seen_at, last_scanned_at, last_changed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            name = excluded.name,
            url = excluded.url,
            final_url = excluded.final_url,
            category = excluded.category,
            parser = excluded.parser,
            auth_required = excluded.auth_required,
            content_hash = excluded.content_hash,
            status_code = excluded.status_code,
            title = excluded.title,
            last_scanned_at = excluded.last_scanned_at,
            last_changed_at = excluded.last_changed_at
        """,
        (
            fetched.source.key,
            fetched.source.name,
            fetched.source.url,
            fetched.final_url,
            fetched.source.category,
            fetched.source.parser,
            int(fetched.source.auth_required),
            fetched.content_hash,
            fetched.status_code,
            fetched.title,
            first_seen_at,
            timestamp,
            last_changed_at,
        ),
    )
    return changed


def sync_source(conn: sqlite3.Connection, run_id: int, fetched: FetchedDocument, *, force: bool, timestamp: str) -> tuple[bool, int, ParsedSource]:
    changed = upsert_source(conn, fetched, timestamp)
    if not changed and not force:
        return False, 0, ParsedSource()

    parser = PARSERS[fetched.source.parser]
    parsed = parser(fetched)
    changes = 0
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_artifacts',
        key_column='item_key',
        records=parsed.artifacts,
        columns=[
            'source_key',
            'item_key',
            'suite_type',
            'android_version',
            'release_name',
            'artifact_kind',
            'arch',
            'file_name',
            'download_url',
            'release_notes_url',
            'user_guide_url',
            'ci_build_id',
            'target_platform',
            'description',
            'section_path',
            'content_hash',
        ],
        entity_type='artifact',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_packages',
        key_column='item_key',
        records=parsed.gms_packages,
        columns=[
            'source_key',
            'item_key',
            'section',
            'android_version',
            'release_notes_url',
            'file_name',
            'download_url',
            'required_from',
            'partner_gerrit_tag',
            'partner_gerrit_url',
            'description',
            'content_hash',
        ],
        entity_type='gms_package',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_requirement_sections',
        key_column='section_key',
        records=parsed.requirement_sections,
        columns=[
            'source_key',
            'section_key',
            'level',
            'number',
            'title',
            'path',
            'text_excerpt',
            'table_count',
            'link_count',
            'content_hash',
        ],
        entity_type='requirement_section',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_requirement_table_rows',
        key_column='row_key',
        records=parsed.requirement_table_rows,
        columns=[
            'source_key',
            'row_key',
            'section_key',
            'section_title',
            'table_index',
            'row_index',
            'headers_json',
            'values_json',
            'row_text',
            'content_hash',
        ],
        entity_type='requirement_table_row',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_requirement_version_tags',
        key_column='tag_key',
        records=parsed.requirement_version_tags,
        columns=[
            'source_key',
            'tag_key',
            'android_version',
            'change_kind',
            'section_key',
            'section_title',
            'requirement_ids',
            'text_excerpt',
            'content_hash',
        ],
        entity_type='requirement_version_tag',
        timestamp=timestamp,
    )
    return True, changes, parsed
