#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from typing import Any

from .models import BulletinPatchRecord, FetchedDocument, ParsedSource
from .parsers import PARSERS, bulletin_content_hash, bulletin_month_identity
from .parsing import matched_excerpt, reference_term_matches


SCHEMA_VERSION = 2


def init_db(conn: sqlite3.Connection, *, manage_transaction: bool = True) -> None:
    if manage_transaction:
        conn.execute('BEGIN IMMEDIATE')
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
        CREATE TABLE IF NOT EXISTS gms_update_bulletin_patches (
            source_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            year TEXT NOT NULL,
            patch_id TEXT NOT NULL,
            patch_kind TEXT NOT NULL DEFAULT '',
            android_versions TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL,
            bulletin_month TEXT NOT NULL,
            deadline TEXT NOT NULL,
            bulletin_url TEXT NOT NULL DEFAULT '',
            detail_text TEXT NOT NULL DEFAULT '',
            detail_status TEXT NOT NULL DEFAULT 'not_scanned',
            detail_fetched_at TEXT NOT NULL DEFAULT '',
            patch_rows_json TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, item_key)
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
    # 旧库升级：bulletin 表补 v2 新列（幂等，按 PRAGMA table_info 判定）。
    bulletin_columns = {
        row[1] for row in conn.execute('PRAGMA table_info(gms_update_bulletin_patches)').fetchall()
    }
    for column, ddl in (
        ('patch_kind', "ALTER TABLE gms_update_bulletin_patches ADD COLUMN patch_kind TEXT NOT NULL DEFAULT ''"),
        ('android_versions', "ALTER TABLE gms_update_bulletin_patches ADD COLUMN android_versions TEXT NOT NULL DEFAULT ''"),
        ('bulletin_url', "ALTER TABLE gms_update_bulletin_patches ADD COLUMN bulletin_url TEXT NOT NULL DEFAULT ''"),
        ('detail_text', "ALTER TABLE gms_update_bulletin_patches ADD COLUMN detail_text TEXT NOT NULL DEFAULT ''"),
        ('detail_status', "ALTER TABLE gms_update_bulletin_patches ADD COLUMN detail_status TEXT NOT NULL DEFAULT 'not_scanned'"),
        ('detail_fetched_at', "ALTER TABLE gms_update_bulletin_patches ADD COLUMN detail_fetched_at TEXT NOT NULL DEFAULT ''"),
        ('patch_rows_json', "ALTER TABLE gms_update_bulletin_patches ADD COLUMN patch_rows_json TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in bulletin_columns:
            conn.execute(ddl)
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
        CREATE TABLE IF NOT EXISTS gms_update_mainline_packages (
            source_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            year TEXT NOT NULL,
            month TEXT NOT NULL,
            month_label TEXT NOT NULL,
            preload_version TEXT NOT NULL,
            notes_url TEXT NOT NULL,
            partner_zip_build_id TEXT NOT NULL,
            ci_build_url TEXT NOT NULL,
            partner_zip_label TEXT NOT NULL,
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
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_mainline_packages_month ON gms_update_mainline_packages(year, month)')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_gms_update_requirement_sections_path '
        'ON gms_update_requirement_sections(level, number, title)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_gms_update_requirement_version_tags_lookup '
        'ON gms_update_requirement_version_tags(android_version, change_kind, section_title)'
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_changes_run ON gms_update_change_events(run_id, source_key)')


def _ensure_bulletin_month_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        'CREATE TABLE IF NOT EXISTS gms_update_bulletin_months ('
        "url TEXT PRIMARY KEY, status TEXT NOT NULL, scanned_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')"
    )
    columns = {row[1] for row in conn.execute('PRAGMA table_info(gms_update_bulletin_months)')}
    if 'reason' not in columns:
        conn.execute("ALTER TABLE gms_update_bulletin_months ADD COLUMN reason TEXT NOT NULL DEFAULT ''")


def bulletin_detail_coverage(conn: sqlite3.Connection) -> dict:
    """Old or partially fetched indexes cannot establish an absence of fixes."""
    columns = {row[1] for row in conn.execute('PRAGMA table_info(gms_update_bulletin_patches)')}
    if 'detail_status' not in columns:
        return {'coverage_complete': False, 'reason': 'Critical Fix detail index needs synchronization'}
    count = conn.execute(
        "SELECT COUNT(*) FROM gms_update_bulletin_patches WHERE detail_status NOT IN ('ready','not_applicable')",
    ).fetchone()[0]
    month_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gms_update_bulletin_months'"
    ).fetchone()
    months, ready = (0, 0)
    failed_months = []
    if month_table:
        months, ready = conn.execute(
            "SELECT COUNT(*), COUNT(CASE WHEN status='ready' THEN 1 END) FROM gms_update_bulletin_months"
        ).fetchone()
        month_columns = {row[1] for row in conn.execute('PRAGMA table_info(gms_update_bulletin_months)')}
        reason = 'reason' if 'reason' in month_columns else "''"
        failed_months = [{'url': row[0], 'reason': row[1] or 'previous scan did not record a reason'}
                         for row in conn.execute(
                             f"SELECT url, {reason} FROM gms_update_bulletin_months WHERE status!='ready' ORDER BY url"
                         )]
    complete = bool(months and ready == months and count == 0)
    return {'coverage_complete': complete, 'incomplete_records': count,
            'months_discovered': months, 'months_scanned': ready, 'failed_months': failed_months,
            **({'reason': 'Critical Fix monthly index needs synchronization or contains unavailable sections'}
               if not complete else {})}


def query_bulletin_patches(conn: sqlite3.Connection, *, q='', year='', kind='', android_version='',
                          limit=100, offset=0) -> dict:
    """Shared page search over patch metadata and complete retained detail text."""
    columns = {row[1] for row in conn.execute('PRAGMA table_info(gms_update_bulletin_patches)')}
    coverage = bulletin_detail_coverage(conn)
    if not columns:
        return {'items': [], 'total': 0, **coverage}
    where, params = [], []
    for column, value in (('year', year), ('patch_kind', kind)):
        if value:
            where.append(f'{column}=?')
            params.append(value)
    if android_version:
        where.append('reference_term_matches(android_versions, ?)')
        params.append(android_version)
    if q:
        searchable = [column for column in ('patch_id', 'description', 'android_versions', 'bulletin_month',
                                            'deadline', 'year', 'patch_kind', 'detail_text') if column in columns]
        where.append('(' + ' OR '.join(f'reference_term_matches({column}, ?)' for column in searchable) + ')')
        params.extend([q] * len(searchable))
    conn.create_function('reference_term_matches', 2, reference_term_matches)
    where_sql = 'WHERE ' + ' AND '.join(where) if where else ''
    total = conn.execute(f'SELECT COUNT(*) FROM gms_update_bulletin_patches {where_sql}', params).fetchone()[0]
    order = 'CAST(year AS INTEGER) DESC, patch_id DESC' if 'year' in columns else 'patch_id DESC'
    if {'year', 'bulletin_month'} <= columns:
        months = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                  'September', 'October', 'November', 'December')
        month_rank = 'CASE bulletin_month ' + ' '.join(
            f"WHEN '{month}' THEN {index}" for index, month in enumerate(months, 1)
        ) + ' ELSE 0 END'
        order = f'CAST(year AS INTEGER) DESC, {month_rank} DESC, patch_id DESC, item_key'
    rows = conn.execute(
        f'SELECT * FROM gms_update_bulletin_patches {where_sql} ORDER BY {order} LIMIT ? OFFSET ?',
        [*params, limit, offset],
    ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item['detail_excerpt'] = matched_excerpt(item.get('detail_text') or '', [q], limit=1200)
    return {'items': items, 'total': total, **coverage}


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
    allow_delete: bool = True,
) -> int:
    existing = {
        row[key_column]: row
        for row in conn.execute(f'SELECT * FROM {table} WHERE source_key = ?', (source_key,)).fetchall()
    }
    incoming = {}
    for record in records:
        key = getattr(record, key_column)
        if key in incoming and incoming[key] != record:
            raise ValueError(f'{table}: conflicting duplicate record key')
        incoming[key] = record
    changes = 0

    for key, row in existing.items():
        if key in incoming or not allow_delete:
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
            metadata = {'content_hash', 'detail_fetched_at', 'first_seen_at', 'last_seen_at',
                        'table_index', 'row_index'}
            if any(before[column] != getattr(record, column) for column in columns
                   if column not in metadata):
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


def sync_source(conn: sqlite3.Connection, run_id: int, fetched: FetchedDocument, *, force: bool, timestamp: str, session: Any = None, mainline_depth: int | None = None, parsed: ParsedSource | None = None) -> tuple[bool, int, ParsedSource]:
    if not 200 <= fetched.status_code < 300:
        raise ValueError(f'{fetched.source.key}: invalid HTTP status; existing records retained')
    changed = upsert_source(conn, fetched, timestamp)
    # Child pages evolve independently of their index.
    if not changed and not force and fetched.source.parser not in {'gms_bulletins', 'mainline_release_notes'}:
        return False, 0, ParsedSource()

    if parsed is None:
        parser = PARSERS[fetched.source.parser]
        if fetched.source.parser == 'mainline_release_notes':
            parsed = parser(fetched, session=session, depth=mainline_depth)
        else:
            parsed = parser(fetched, session=session)
    primary = {
        'cts_downloads': parsed.artifacts, 'vts_downloads': parsed.artifacts,
        'gts_downloads': parsed.artifacts, 'gms_downloads': parsed.gms_packages,
        'gms_requirements': parsed.requirement_sections,
        'mainline_release_notes': parsed.mainline_packages,
    }
    if fetched.source.parser in primary and not primary[fetched.source.parser]:
        raise ValueError(f'{fetched.source.key}: no records parsed; existing records retained')
    if fetched.source.parser == 'gms_bulletins':
        if not parsed.bulletin_patches:
            raise ValueError('Critical Fix overview contains no patch records; existing records were retained')
        previous = {row['item_key']: dict(row) for row in conn.execute(
            'SELECT * FROM gms_update_bulletin_patches WHERE source_key=?', (fetched.source.key,),
        )}
        retained = []
        for patch in parsed.bulletin_patches:
            old = previous.get(patch.item_key)
            if patch.detail_status != 'ready' and old and old['detail_text']:
                patch = replace(
                    patch, detail_text=old['detail_text'], detail_status='stale',
                    detail_fetched_at=old['detail_fetched_at'],
                    patch_rows_json=old['patch_rows_json'],
                    patch_kind=patch.patch_kind or old['patch_kind'],
                    android_versions=patch.android_versions or old['android_versions'],
                )
                patch = replace(patch, content_hash=bulletin_content_hash(patch))
            retained.append(patch)
        # A failed or missing monthly page must not delete existing evidence.
        _ensure_bulletin_month_index(conn)
        prior_months = dict(conn.execute('SELECT url, status FROM gms_update_bulletin_months'))
        statuses = dict(parsed.bulletin_month_status)
        diagnostics = dict(parsed.bulletin_month_diagnostics)
        by_identity = {bulletin_month_identity(url): url for url in statuses if bulletin_month_identity(url)}
        for url in prior_months:
            canonical = by_identity.get(bulletin_month_identity(url))
            if canonical and canonical != url:
                # An abbreviated/full month URL can be redirected or renamed.
                # Do not let an obsolete alias count as an unscanned month.
                conn.execute('DELETE FROM gms_update_bulletin_months WHERE url=?', (url,))
                continue
            if url not in statuses:
                statuses[url] = 'unavailable'
                diagnostics[url] = 'previously indexed month not found in current navigation'
        incoming_keys = {patch.item_key for patch in retained}
        ready_records = {(patch.year, patch.bulletin_month.casefold(), patch.patch_id)
                         for patch in retained if patch.detail_status == 'ready'}
        for key, old in previous.items():
            if key in incoming_keys:
                continue
            # A successful replacement of the same month and patch supersedes
            # its legacy key even when another entry in that month is missing.
            identity = (old['year'], old['bulletin_month'].casefold(), old['patch_id'])
            if old['patch_id'] and identity in ready_records:
                continue
            url = str(old.get('bulletin_url') or '').split('#', 1)[0].split('?', 1)[0].rstrip('/')
            url = by_identity.get(bulletin_month_identity(url), url)
            if statuses.get(url) == 'ready':
                statuses[url] = 'unavailable'
                diagnostics[url] = 'previously indexed patch missing: ' + old['patch_id']
            elif url in statuses:
                diagnostics[url] = (diagnostics.get(url, '') + '; retained patch: ' + old['patch_id']).strip('; ')
            record = BulletinPatchRecord(**{
                name: old[name] for name in BulletinPatchRecord.__dataclass_fields__
            })
            retained.append(replace(record, detail_status='stale'))
        for url, status in statuses.items():
            conn.execute(
                'INSERT INTO gms_update_bulletin_months (url,status,scanned_at,reason) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(url) DO UPDATE SET status=excluded.status, scanned_at=excluded.scanned_at, reason=excluded.reason',
                (url, status, timestamp, diagnostics.get(url, '')),
            )
        parsed.bulletin_patches = retained
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
        table='gms_update_mainline_packages',
        key_column='item_key',
        records=parsed.mainline_packages,
        columns=[
            'source_key',
            'item_key',
            'year',
            'month',
            'month_label',
            'preload_version',
            'notes_url',
            'partner_zip_build_id',
            'ci_build_url',
            'partner_zip_label',
            'content_hash',
        ],
        entity_type='mainline_package',
        timestamp=timestamp,
        allow_delete=False,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_bulletin_patches',
        key_column='item_key',
        records=parsed.bulletin_patches,
        columns=[
            'source_key',
            'item_key',
            'year',
            'patch_id',
            'patch_kind',
            'android_versions',
            'description',
            'bulletin_month',
            'deadline',
            'bulletin_url',
            'detail_text',
            'detail_status',
            'detail_fetched_at',
            'patch_rows_json',
            'content_hash',
        ],
        entity_type='gms_bulletin',
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
