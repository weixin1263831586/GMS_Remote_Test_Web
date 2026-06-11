#!/usr/bin/env python3
"""Sync Mainline known issues from Android Partner release notes."""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin

import requests
from lxml import html

import fetch_partner_android


DEFAULT_INDEX_URL = 'https://docs.partner.android.com/mainline/release/release-notes?authuser=2'
DEFAULT_DB_PATH = Path('data/mainline_known_issues.sqlite3')
KNOWN_ISSUE_HEADING_RE = re.compile(r'^(MTS|CTS|GTS)\s+known issues\b.*:$', flags=re.IGNORECASE)
PRODUCT_SECTIONS = ('Android', 'Android Go')


@dataclass(frozen=True)
class ReleasePage:
    year: int
    label: str
    url: str


@dataclass(frozen=True)
class FetchedPage:
    doc: html.HtmlElement
    content_hash: str
    status_code: int
    final_url: str


@dataclass(frozen=True)
class KnownIssue:
    source_url: str
    source_title: str
    release_year: int
    release_label: str
    product_section: str
    issue_type: str
    android_versions: str
    category: str
    test_module: str
    test_case: str
    exemption_id: str
    issue_text: str


def build_session(args: argparse.Namespace) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            'User-Agent': fetch_partner_android.USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    )
    loaded = fetch_partner_android.load_browser_cookies(
        session,
        url=args.index_url,
        cookie_file=args.cookie_file,
        cookie_header=args.cookie_header,
        browser=args.browser,
        verbose=args.verbose,
    )
    if not loaded:
        print('warning: no browser cookies loaded; Partner pages may fail authentication', file=sys.stderr)
    return session


def fetch_page(session: requests.Session, url: str, timeout: float) -> FetchedPage:
    response = session.get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    doc = html.fromstring(response.content)
    content_hash = stable_doc_hash(doc)
    return FetchedPage(
        doc=doc,
        content_hash=content_hash,
        status_code=response.status_code,
        final_url=response.url,
    )


def fetch_doc(session: requests.Session, url: str, timeout: float) -> html.HtmlElement:
    return fetch_page(session, url, timeout).doc


def text_content(node: html.HtmlElement) -> str:
    return ' '.join(' '.join(node.xpath('.//text()')).split())


def stable_doc_hash(doc: html.HtmlElement) -> str:
    doc_copy = html.fromstring(html.tostring(doc))
    for node in doc_copy.xpath('//script|//style|//noscript'):
        node.drop_tree()
    articles = doc_copy.xpath('//article[contains(@class, "devsite-article")]')
    target = articles[0] if articles else doc_copy
    stable_text = text_content(target)
    return hashlib.sha256(stable_text.encode('utf-8')).hexdigest()


def direct_text(node: html.HtmlElement) -> str:
    return ' '.join(' '.join(node.xpath('./text()')).split())


def extract_release_years(index_doc: html.HtmlElement) -> list[int]:
    nav_nodes = index_doc.xpath('//nav[contains(@class, "devsite-book-nav")]')
    if not nav_nodes:
        raise RuntimeError('left navigation bar not found: devsite-book-nav')
    years: list[int] = []
    seen: set[int] = set()
    for node in nav_nodes[0].xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " devsite-nav-title-no-path ")]'
    ):
        match = re.fullmatch(r'(\d{4}) Build Release Notes', text_content(node))
        if not match:
            continue
        year = int(match.group(1))
        if year in seen:
            continue
        seen.add(year)
        years.append(year)
    return years


def extract_release_pages(index_doc: html.HtmlElement, index_url: str, year: int) -> list[ReleasePage]:
    nav_nodes = index_doc.xpath('//nav[contains(@class, "devsite-book-nav")]')
    if not nav_nodes:
        raise RuntimeError('left navigation bar not found: devsite-book-nav')
    year_title = f'{year} Build Release Notes'
    year_nodes = nav_nodes[0].xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " devsite-nav-title-no-path ") '
        'and normalize-space(.)=$title]',
        title=year_title,
    )
    if not year_nodes:
        raise RuntimeError(f'left navigation section not found: {year_title}')

    section = year_nodes[0].getparent().xpath('./ul[contains(@class, "devsite-nav-section")]')
    if not section:
        raise RuntimeError(f'left navigation section has no links: {year_title}')

    pages: list[ReleasePage] = []
    seen: set[str] = set()
    for link in section[0].xpath('.//a[@href]'):
        url = urldefrag(urljoin(index_url, link.get('href')))[0]
        if url in seen:
            continue
        seen.add(url)
        label = text_content(link) or url.rsplit('/', 1)[-1]
        pages.append(ReleasePage(year=year, label=label, url=url))
    return pages


def heading_level(node: html.HtmlElement) -> int:
    if not re.fullmatch(r'h[1-6]', node.tag):
        return 0
    return int(node.tag[1])


def find_section_nodes(article: html.HtmlElement, heading_text: str) -> list[html.HtmlElement]:
    headings = article.xpath(
        './/*[self::h2 or self::h3 or self::h4 or self::h5 or self::h6][normalize-space(.)=$heading]',
        heading=heading_text,
    )
    if not headings:
        return []
    heading = headings[0]
    level = heading_level(heading)
    nodes: list[html.HtmlElement] = []
    for sibling in heading.itersiblings():
        sibling_level = heading_level(sibling)
        if sibling_level and sibling_level <= level:
            break
        nodes.append(sibling)
    return nodes


def find_known_issue_lists(section_nodes: list[html.HtmlElement]) -> list[tuple[str, html.HtmlElement]]:
    lists: list[tuple[str, html.HtmlElement]] = []
    for node in section_nodes:
        for strong in node.xpath('.//strong'):
            match = KNOWN_ISSUE_HEADING_RE.match(text_content(strong))
            if not match:
                continue
            parent = strong
            while parent is not None and parent.tag != 'li':
                parent = parent.getparent()
            if parent is None:
                continue
            lists.extend((match.group(1).upper(), issue_list) for issue_list in parent.xpath('./ul'))
    return lists


def parse_issue_item(
    item: html.HtmlElement,
    *,
    page: ReleasePage,
    title: str,
    product_section: str,
    issue_type: str,
) -> list[KnownIssue]:
    issue_text = text_content(item)
    if 'internal bug ref.' not in issue_text:
        return []

    bug_match = re.search(r'internal bug ref\.\s*([0-9,\sand]+)', issue_text, flags=re.IGNORECASE)
    if not bug_match:
        return []
    exemption_ids = re.findall(r'\d+', bug_match.group(1))
    if not exemption_ids:
        return []

    prefix_match = re.match(r'\[([^\]]+)\](?:\[([^\]]+)\])?', issue_text)
    android_versions = prefix_match.group(1).strip() if prefix_match else ''
    category = prefix_match.group(2).strip() if prefix_match and prefix_match.group(2) else ''

    codes = [text_content(code) for code in item.xpath('./code|./p/code|./strong/code')]
    nested_codes = [text_content(code) for code in item.xpath('./ul//code')]
    if not codes:
        all_codes = [text_content(code) for code in item.xpath('.//code')]
        codes = [code for code in all_codes if '#' not in code]
        nested_codes = [code for code in all_codes if '#' in code]

    test_modules = [code for code in codes if code and '#' not in code]
    test_cases = [code for code in nested_codes if '#' in code]
    if not test_modules or not test_cases:
        return []

    issues: list[KnownIssue] = []
    for exemption_id in exemption_ids:
        for test_module in test_modules:
            for test_case in test_cases:
                issues.append(
                    KnownIssue(
                        source_url=page.url,
                        source_title=title,
                        release_year=page.year,
                        release_label=page.label,
                        product_section=product_section,
                        issue_type=issue_type,
                        android_versions=android_versions,
                        category=category,
                        test_module=test_module,
                        test_case=test_case,
                        exemption_id=exemption_id,
                        issue_text=issue_text,
                    )
                )
    return issues


def extract_known_issues(page: ReleasePage, doc: html.HtmlElement) -> list[KnownIssue]:
    title = ' '.join(doc.xpath('//title/text()')).strip()
    articles = doc.xpath('//article[contains(@class, "devsite-article")]')
    article = articles[0] if articles else doc
    issues: list[KnownIssue] = []

    for product_section in PRODUCT_SECTIONS:
        section_nodes = find_section_nodes(article, product_section)
        for issue_type, issue_list in find_known_issue_lists(section_nodes):
            for item in issue_list.xpath('./li'):
                issues.extend(
                    parse_issue_item(
                        item,
                        page=page,
                        title=title,
                        product_section=product_section,
                        issue_type=issue_type,
                    )
                )
    return issues


def issue_dedupe_key(issue: KnownIssue) -> tuple[str, str, str, str, str]:
    return (
        issue.product_section,
        issue.issue_type,
        issue.test_module,
        issue.test_case,
        issue.exemption_id,
    )


def dedupe_issues(issues: list[KnownIssue], known_keys: set[tuple[str, str, str, str, str]]) -> list[KnownIssue]:
    unique: list[KnownIssue] = []
    for issue in issues:
        key = issue_dedupe_key(issue)
        if key in known_keys:
            continue
        known_keys.add(key)
        unique.append(issue)
    return unique


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
