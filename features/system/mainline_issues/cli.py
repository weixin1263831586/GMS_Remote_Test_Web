#!/usr/bin/env python3
"""Sync Mainline known issues from Android Partner release notes."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


DEFAULT_INDEX_URL = 'https://docs.partner.android.com/mainline/release/release-notes?authuser=2'
DEFAULT_DB_PATH = Path('data/mainline_known_issues.sqlite3')
KNOWN_ISSUE_HEADING_RE = re.compile(r'^(MTS|CTS|GTS)\s+known issues\b.*:$', flags=re.IGNORECASE)
PRODUCT_SECTIONS = ('Android', 'Android Go')



from .parser import *
from .repository import *


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Scan Android Partner Mainline release notes and store known issue exemptions.',
    )
    parser.add_argument('--index-url', default=DEFAULT_INDEX_URL, help=f'Default: {DEFAULT_INDEX_URL}')
    parser.add_argument(
        '--year',
        type=int,
        action='append',
        help='Release-note year to scan. Can be repeated. Default: all years found in the left nav.',
    )
    parser.add_argument('--db', type=Path, default=DEFAULT_DB_PATH, help=f'Default: {DEFAULT_DB_PATH}')
    parser.add_argument('--timeout', type=float, default=30.0, help='Request timeout in seconds. Default: 30.')
    parser.add_argument('--dry-run', action='store_true', help='Parse and print results without writing SQLite.')
    parser.add_argument('--force', action='store_true', help='Re-parse and rewrite pages even when content hash is unchanged.')
    parser.add_argument('--new-only', action='store_true', help='Only fetch pages that are not recorded in the page state table.')
    parser.add_argument('--browser', choices=('auto', 'firefox', 'chromium'), default='auto')
    parser.add_argument('--cookie-file', type=Path, help='Netscape-format cookie file fallback.')
    parser.add_argument('--cookie-header', help='Manual Cookie header fallback.')
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def main() -> int:
    args = build_parser().parse_args()
    started_at = datetime.now(timezone.utc).isoformat()
    session = build_session(args)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.commit()

    try:
        index_doc = fetch_doc(session, args.index_url, args.timeout)
    except requests.RequestException as exc:
        print(f'error: failed to fetch index page {args.index_url}: {exc}', file=sys.stderr)
        return 1
    years = args.year or extract_release_years(index_doc)
    if not years:
        raise RuntimeError('no Build Release Notes years found in left navigation')

    pages: list[ReleasePage] = []
    for year in years:
        pages.extend(extract_release_pages(index_doc, args.index_url, year))
    if args.verbose:
        years_text = ', '.join(str(year) for year in years)
        print(f'found {len(pages)} release-note pages under years: {years_text}', file=sys.stderr)

    all_issues: list[KnownIssue] = []
    pages_scanned = 0
    pages_skipped = 0
    page_updates: list[tuple[ReleasePage, FetchedPage, list[KnownIssue], bool]] = []
    known_issue_keys: set = set()
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        if not args.force:
            known_issue_keys = load_known_issue_keys(conn)
    for idx, page in enumerate(pages, 1):
        with sqlite3.connect(args.db) as conn:
            conn.row_factory = sqlite3.Row
            page_state = get_page_state(conn, page.url)
        if args.new_only and page_state:
            pages_skipped += 1
            if args.verbose:
                print(f'{idx}/{len(pages)} skipped existing {page.label} {page.url}', file=sys.stderr)
            continue

        try:
            fetched = fetch_page(session, page.url, args.timeout)
        except requests.RequestException as exc:
            print(f'warning: skipped {page.url}: {exc}', file=sys.stderr)
            continue

        unchanged = bool(page_state and page_state['content_hash'] == fetched.content_hash)
        if unchanged and not args.force:
            pages_skipped += 1
            if not args.dry_run:
                with sqlite3.connect(args.db) as conn:
                    conn.row_factory = sqlite3.Row
                    upsert_page_state(
                        conn,
                        page,
                        fetched,
                        int(page_state['issues_found']),
                        datetime.now(timezone.utc).isoformat(),
                        changed=False,
                    )
                    conn.commit()
            if args.verbose:
                print(f'{idx}/{len(pages)} skipped unchanged {page.label} {page.url}', file=sys.stderr)
            continue

        raw_issues = extract_known_issues(page, fetched.doc)
        if not args.force:
            with sqlite3.connect(args.db) as conn:
                conn.row_factory = sqlite3.Row
                known_issue_keys = load_known_issue_keys(conn, exclude_source_url=page.url)
        issues = dedupe_issues(raw_issues, known_issue_keys)
        all_issues.extend(issues)
        page_updates.append((page, fetched, issues, not unchanged))
        pages_scanned += 1
        if args.verbose:
            suffix = f' ({len(raw_issues) - len(issues)} duplicate skipped)' if len(raw_issues) != len(issues) else ''
            print(f'{idx}/{len(pages)} {len(issues)} issues{suffix} {page.label} {page.url}', file=sys.stderr)

    if args.dry_run:
        for issue in all_issues:
            print(
                '\t'.join(
                    [
                        issue.product_section,
                        issue.issue_type,
                        str(issue.release_year),
                        issue.release_label,
                        issue.exemption_id,
                        issue.test_module,
                        issue.test_case,
                        issue.source_url,
                    ]
                )
            )
        print(f'total_pages={len(pages)} pages_scanned={pages_scanned} pages_skipped={pages_skipped} issues_found={len(all_issues)}')
        return 0

    finished_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        if args.force:
            conn.execute('DELETE FROM mainline_known_issues')
        for page, fetched, issues, changed in page_updates:
            replace_page_issues(conn, page.url, issues, finished_at)
            upsert_page_state(conn, page, fetched, len(issues), finished_at, changed=changed)
        conn.execute(
            """
            INSERT INTO mainline_known_issue_sync_runs (
                index_url, release_year, pages_scanned, pages_skipped, issues_found, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.index_url,
                years[0] if len(years) == 1 else None,
                pages_scanned,
                pages_skipped,
                len(all_issues),
                started_at,
                finished_at,
            ),
        )
        conn.commit()

    print(
        f'total_pages={len(pages)} pages_scanned={pages_scanned} '
        f'pages_skipped={pages_skipped} issues_found={len(all_issues)} db={args.db}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
