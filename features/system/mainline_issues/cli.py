#!/usr/bin/env python3
"""Sync Mainline known issues from Android Partner release notes."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests

from foundation import partner_android as fetch_partner_android

from ..reference_sync import mainline_scanner, run_reference_sync
from .parser import (
    DEFAULT_INDEX_URL,
    FetchedPage,
    KnownIssue,
    ReleasePage,
    SecurityPatch,
    build_session,
    dedupe_issues,
    extract_known_issues,
    extract_release_pages,
    extract_release_years,
    extract_security_patches,
    fetch_doc,
    fetch_page,
)
from .repository import (
    get_page_state,
    init_db,
    load_known_issue_keys,
    replace_page_patches,
    upsert_issues,
    upsert_page_state,
)


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
    parser.add_argument('--sync-lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--db', type=Path, default=mainline_scanner.db_path, help=f'Default: {mainline_scanner.db_path}')
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
    return run_reference_sync(build_parser().parse_args(), run_sync)


def run_sync(args: argparse.Namespace) -> int:
    if not args.dry_run:
        args.db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(':memory:' if args.dry_run else args.db)) as conn:
        if args.dry_run and args.db.is_file():
            with closing(sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                source.backup(conn)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.commit()
        with build_session(args) as session:
            return _scan(args, conn, session)


def _scan(args: argparse.Namespace, conn: sqlite3.Connection, session: requests.Session) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        index_doc = fetch_doc(session, args.index_url, args.timeout)
    except requests.RequestException as exc:
        response = getattr(exc, 'response', None)
        manual_hint = (
            'sign in to docs.partner.android.com with your Google account in host Firefox '
            '(the page does not need to stay open), then re-run the sync'
        )
        if response is not None and fetch_partner_android.is_auth_failure(response.status_code, str(response.url)):
            if fetch_partner_android.launch_login_browser(args.index_url, reason=f'index HTTP {response.status_code}'):
                manual_hint = 'opened the page in host Firefox; sign in there, then re-run the sync'
        print(f'error: failed to fetch index page {args.index_url}: {exc}; {manual_hint}', file=sys.stderr)
        return 1
    years = args.year or extract_release_years(index_doc)
    if not years:
        raise RuntimeError('no Build Release Notes years found in left navigation')

    pages: list[ReleasePage] = []
    for year in years:
        year_pages = extract_release_pages(index_doc, args.index_url, year)
        if not year_pages:
            print(f'error: no release-note pages for {year}; retaining existing records', file=sys.stderr)
            return 1
        pages.extend(year_pages)
    if args.verbose:
        years_text = ', '.join(str(year) for year in years)
        print(f'found {len(pages)} release-note pages under years: {years_text}', file=sys.stderr)

    all_issues: list[KnownIssue] = []
    patches_found = 0
    pages_scanned = 0
    pages_skipped = 0
    auth_failures = 0
    page_updates: list[tuple[ReleasePage, FetchedPage, list[KnownIssue], list[SecurityPatch], bool]] = []
    rebuild_all = args.force and not args.year and not args.new_only
    fetch_failures = 0
    for idx, page in enumerate(pages, 1):
        page_state = get_page_state(conn, page.url)
        if args.new_only and page_state:
            pages_skipped += 1
            if args.verbose:
                print(f'{idx}/{len(pages)} skipped existing {page.label} {page.url}', file=sys.stderr)
            continue

        try:
            fetched = fetch_page(session, page.url, args.timeout)
        except requests.RequestException as exc:
            fetch_failures += 1
            # 扫描中途 Cookie 失效表现为子页连续 401/403 或跳到登录页
            # （单页 404 可能是真实下线页，不计入）。连续 3 次判定为认证
            # 失效并中止本轮，避免整轮全 404；默认不再自动弹桌面浏览器，
            # 提示手动登录（GMS_PARTNER_ANDROID_AUTO_LOGIN=1 恢复弹窗）。
            response = getattr(exc, 'response', None)
            if response is not None and fetch_partner_android.is_auth_failure(
                    response.status_code, str(response.url), allow_404=False):
                auth_failures += 1
                if auth_failures >= 3:
                    launched = fetch_partner_android.launch_login_browser(
                        page.url, reason=f'{auth_failures} consecutive auth failures')
                    hint = (
                        'opened the login page in host Firefox'
                        if launched else
                        'sign in to docs.partner.android.com with your Google account in host Firefox'
                    )
                    print(
                        f'error: consecutive Partner authentication failures; {hint}. '
                        'Sign in there, then re-run the sync.',
                        file=sys.stderr,
                    )
                    return 1
            else:
                auth_failures = 0
            print(f'warning: skipped {page.url}: {exc}', file=sys.stderr)
            continue

        auth_failures = 0

        unchanged = bool(page_state and page_state['content_hash'] == fetched.content_hash)
        if unchanged and not args.force:
            pages_skipped += 1
            if not args.dry_run:
                with conn:
                    upsert_page_state(
                        conn, page, fetched, int(page_state['issues_found']),
                        datetime.now(timezone.utc).isoformat(), changed=False,
                    )
            if args.verbose:
                print(f'{idx}/{len(pages)} skipped unchanged {page.label} {page.url}', file=sys.stderr)
            continue

        raw_issues = extract_known_issues(page, fetched.doc)
        raw_patches = extract_security_patches(page, fetched.doc)
        patches_found += len(raw_patches)
        page_updates.append((page, fetched, raw_issues, raw_patches, not unchanged))
        pages_scanned += 1
        if args.verbose:
            print(f'{idx}/{len(pages)} parsed {len(raw_issues)} issues, {len(raw_patches)} security patches '
                  f'{page.label} {page.url}', file=sys.stderr)

    if fetch_failures:
        print(f'error: {fetch_failures} release-note pages unavailable; retaining existing records', file=sys.stderr)
        return 1

    finished_at = datetime.now(timezone.utc).isoformat()
    with conn:
        if rebuild_all:
            conn.execute('DELETE FROM mainline_known_issues')
            conn.execute('DELETE FROM mainline_security_patches')
            active_urls = {page.url for page in pages}
            stale_urls = [(row[0],) for row in conn.execute('SELECT source_url FROM mainline_release_note_pages')
                          if row[0] not in active_urls]
            conn.executemany('DELETE FROM mainline_release_note_pages WHERE source_url=?', stale_urls)
        else:
            # A shared issue key can move between two updated pages in the same scan.
            conn.executemany('DELETE FROM mainline_known_issues WHERE source_url=?',
                             [(page.url,) for page, *_ in page_updates])
        known_keys = load_known_issue_keys(conn)
        for page, fetched, raw_issues, patches, changed in page_updates:
            issues = dedupe_issues(raw_issues, known_keys)
            all_issues.extend(issues)
            upsert_issues(conn, issues, finished_at)
            replace_page_patches(conn, page.url, patches, finished_at)
            upsert_page_state(conn, page, fetched, len(issues), finished_at, changed=changed)
        conn.execute(
            """
            INSERT INTO mainline_known_issue_sync_runs (
                index_url, release_year, pages_scanned, pages_skipped, issues_found,
                patches_found, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.index_url,
                years[0] if len(years) == 1 else None,
                pages_scanned,
                pages_skipped,
                len(all_issues),
                patches_found,
                started_at,
                finished_at,
            ),
        )
        conn.commit()

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
        print(f'total_pages={len(pages)} pages_scanned={pages_scanned} pages_skipped={pages_skipped} issues_found={len(all_issues)} security_patches_found={patches_found}')
        return 0

    print(
        f'total_pages={len(pages)} pages_scanned={pages_scanned} '
        f'pages_skipped={pages_skipped} issues_found={len(all_issues)} '
        f'security_patches_found={patches_found} db={args.db}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
