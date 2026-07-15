#!/usr/bin/env python3
"""Fetch Android Partner docs with browser cookies when available."""

from __future__ import annotations

import argparse
import http.cookiejar
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests


DEFAULT_URL = os.getenv(
    'GMS_PARTNER_ANDROID_URL',
    'https://docs.partner.android.com/mainline/release/release-notes',
)
COOKIE_HOST_SUFFIXES = (
    '.android.com',
    '.google.com',
    '.googleusercontent.com',
)
USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
)


def _candidate_firefox_cookie_dbs() -> list[Path]:
    home = Path.home()
    roots = [
        home / '.mozilla' / 'firefox',
        home / 'snap' / 'firefox' / 'common' / '.mozilla' / 'firefox',
        home / '.var' / 'app' / 'org.mozilla.firefox' / '.mozilla' / 'firefox',
    ]
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        paths.extend(root.glob('*.default*/cookies.sqlite'))
        paths.extend(root.glob('*.default-release/cookies.sqlite'))
        paths.extend(root.glob('*.default/cookies.sqlite'))
    return _existing_unique(paths)


def _candidate_chromium_cookie_dbs() -> list[Path]:
    home = Path.home()
    roots = [
        home / '.config' / 'google-chrome',
        home / '.config' / 'chromium',
        home / '.config' / 'BraveSoftware' / 'Brave-Browser',
        home / '.config' / 'microsoft-edge',
        home / 'snap' / 'chromium' / 'common' / 'chromium',
        home / '.var' / 'app' / 'com.google.Chrome' / 'config' / 'google-chrome',
        home / '.var' / 'app' / 'org.chromium.Chromium' / 'config' / 'chromium',
    ]
    profile_names = ('Default', 'Profile *')
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for profile_name in profile_names:
            for profile in root.glob(profile_name):
                paths.append(profile / 'Network' / 'Cookies')
                paths.append(profile / 'Cookies')
    return _existing_unique(paths)


def _existing_unique(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _domain_in_scope(host: str) -> bool:
    normalized = host.lstrip('.').lower()
    return any(normalized == suffix.lstrip('.') or normalized.endswith(suffix) for suffix in COOKIE_HOST_SUFFIXES)


def _read_sqlite_rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    with tempfile.TemporaryDirectory(prefix='browser-cookies-') as tmp_dir:
        snapshot = Path(tmp_dir) / 'cookies.sqlite'
        shutil.copy2(db_path, snapshot)
        conn = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(sql))
        finally:
            conn.close()


def _load_firefox_cookies(session: requests.Session, db_path: Path) -> int:
    rows = _read_sqlite_rows(
        db_path,
        """
        SELECT host, path, isSecure, expiry, name, value
        FROM moz_cookies
        WHERE host LIKE '%android.com'
           OR host LIKE '%google.com'
           OR host LIKE '%googleusercontent.com'
        """,
    )
    count = 0
    for row in rows:
        host = row['host']
        if not host or not _domain_in_scope(host):
            continue
        session.cookies.set(
            row['name'],
            row['value'],
            domain=host,
            path=row['path'] or '/',
            secure=bool(row['isSecure']),
            expires=int(row['expiry']) if row['expiry'] else None,
        )
        count += 1
    return count


def _load_chromium_cookies(session: requests.Session, db_path: Path) -> int:
    rows = _read_sqlite_rows(
        db_path,
        """
        SELECT host_key, path, is_secure, expires_utc, name, value, encrypted_value
        FROM cookies
        WHERE host_key LIKE '%android.com'
           OR host_key LIKE '%google.com'
           OR host_key LIKE '%googleusercontent.com'
        """,
    )
    count = 0
    for row in rows:
        host = row['host_key']
        value = row['value']
        if not host or not value or not _domain_in_scope(host):
            continue
        session.cookies.set(
            row['name'],
            value,
            domain=host,
            path=row['path'] or '/',
            secure=bool(row['is_secure']),
        )
        count += 1
    return count


def _load_netscape_cookie_file(session: requests.Session, cookie_file: Path) -> int:
    jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
    jar.load(ignore_discard=True, ignore_expires=True)
    count = 0
    for cookie in jar:
        if not _domain_in_scope(cookie.domain):
            continue
        session.cookies.set_cookie(cookie)
        count += 1
    return count


def _load_manual_cookie_header(session: requests.Session, url: str, cookie_header: str) -> int:
    parsed = urlparse(url)
    count = 0
    for item in cookie_header.split(';'):
        if '=' not in item:
            continue
        name, value = item.split('=', 1)
        name = name.strip()
        if not name:
            continue
        session.cookies.set(name, value.strip(), domain=parsed.hostname, path='/')
        count += 1
    return count


def load_browser_cookies(
    session: requests.Session,
    *,
    url: str,
    cookie_file: Path | None,
    cookie_header: str | None,
    browser: str,
    verbose: bool,
) -> int:
    loaded = 0
    if cookie_file:
        count = _load_netscape_cookie_file(session, cookie_file.expanduser())
        if verbose:
            print(f'loaded {count} cookies from {cookie_file}', file=sys.stderr)
        return count

    if cookie_header:
        count = _load_manual_cookie_header(session, url, cookie_header)
        if verbose:
            print(f'loaded {count} cookies from manual Cookie header', file=sys.stderr)
        return count

    if browser in ('auto', 'firefox'):
        for db_path in _candidate_firefox_cookie_dbs():
            try:
                count = _load_firefox_cookies(session, db_path)
            except (OSError, sqlite3.Error) as exc:
                if verbose:
                    print(f'skipped Firefox cookies {db_path}: {exc}', file=sys.stderr)
                continue
            loaded += count
            if verbose:
                print(f'loaded {count} Firefox cookies from {db_path}', file=sys.stderr)
            if count:
                return loaded

    if browser in ('auto', 'chromium'):
        for db_path in _candidate_chromium_cookie_dbs():
            try:
                count = _load_chromium_cookies(session, db_path)
            except (OSError, sqlite3.Error) as exc:
                if verbose:
                    print(f'skipped Chromium cookies {db_path}: {exc}', file=sys.stderr)
                continue
            loaded += count
            if verbose:
                print(f'loaded {count} Chromium plain cookies from {db_path}', file=sys.stderr)
            if count:
                return loaded

    return loaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Fetch Android Partner docs, preferring local browser cookies for authenticated access.',
    )
    parser.add_argument('url', nargs='?', default=DEFAULT_URL, help=f'URL to fetch. Default: {DEFAULT_URL}')
    parser.add_argument('-o', '--output', help='Write response body to this file instead of stdout.')
    parser.add_argument(
        '--browser',
        choices=('auto', 'firefox', 'chromium'),
        default='auto',
        help='Browser cookie store to try first. Default: auto.',
    )
    parser.add_argument('--cookie-file', type=Path, help='Netscape-format cookie file fallback.')
    parser.add_argument('--cookie-header', help='Manual Cookie header fallback, for one-off debugging.')
    parser.add_argument('--timeout', type=float, default=30.0, help='Request timeout in seconds. Default: 30.')
    parser.add_argument('--status-only', action='store_true', help='Only print final URL and HTTP status.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Print cookie source diagnostics to stderr.')
    return parser


def main() -> int:
    args = build_parser().parse_args()
    session = requests.Session()
    session.headers.update(
        {
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    )

    loaded = load_browser_cookies(
        session,
        url=args.url,
        cookie_file=args.cookie_file,
        cookie_header=args.cookie_header,
        browser=args.browser,
        verbose=args.verbose,
    )
    if not loaded:
        print(
            'warning: no browser cookies loaded; login-only pages may redirect or return an auth error',
            file=sys.stderr,
        )

    response = session.get(args.url, timeout=args.timeout, allow_redirects=True)
    if args.status_only:
        print(f'{response.status_code} {response.url}')
        return 0 if response.ok else 1

    if args.output:
        Path(args.output).expanduser().write_bytes(response.content)
    else:
        sys.stdout.buffer.write(response.content)
    return 0 if response.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
