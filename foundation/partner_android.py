#!/usr/bin/env python3
"""Fetch Android Partner docs with browser cookies when available."""

from __future__ import annotations

import argparse
import http.cookiejar
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
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
        # '*.default*' 已覆盖 default-release/default 等全部变体。
        paths.extend(root.glob('*.default*/cookies.sqlite'))
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


def is_auth_failure(status_code: int, final_url: str, *, allow_404: bool = True) -> bool:
    """True when a Partner docs fetch looks like a lost Google session.

    Signals: any redirect to accounts.google.com, HTTP 401/403, or the
    auth-walled 404 the site serves to anonymous clients. Real missing
    pages also return 404, so per-child-page callers pass allow_404=False
    and only auth statuses / login redirects count.
    """
    host = (urlparse(final_url or '').hostname or '').lower()
    if host.endswith('accounts.google.com'):
        return True
    if status_code in (401, 403):
        return True
    if status_code == 404 and allow_404:
        return host.endswith('partner.android.com')
    return False


AUTO_LOGIN_ENV = 'GMS_PARTNER_ANDROID_AUTO_LOGIN'
AUTO_LOGIN_DISPLAY_ENV = 'GMS_PARTNER_AUTO_LOGIN_DISPLAY'
AUTO_LOGIN_MARKER_ENV = 'GMS_PARTNER_ANDROID_AUTO_LOGIN_MARKER'
AUTO_LOGIN_DEBOUNCE_SECONDS = 1800


def _auto_login_enabled() -> bool:
    # 默认关闭：认证失败只在扫描结果里提示手动登录，避免服务端
    # 擅自在桌面弹 Firefox 窗口。需要旧的自愈行为时显式设为 1。
    return os.getenv(AUTO_LOGIN_ENV, '0').strip().lower() in {'1', 'true', 'yes', 'on'}


def _auto_login_marker_path() -> Path:
    return Path(os.getenv(AUTO_LOGIN_MARKER_ENV) or (Path(tempfile.gettempdir()) / 'gms-partner-login-launch'))


def _auto_login_display_environment():
    """X11 environment for the host desktop session, or None when absent.

    Display defaults to :0 (the gdm/Xorg session x11vnc shares); the X
    socket probe avoids popping windows on headless service hosts.
    """
    display = os.getenv(AUTO_LOGIN_DISPLAY_ENV, ':0')
    socket_path = Path('/tmp/.X11-unix') / ('X' + display.lstrip(':').split('.')[0])
    if not socket_path.exists():
        return None
    env = dict(os.environ)
    env['DISPLAY'] = display
    xauth = os.getenv('XAUTHORITY')
    if not xauth:
        candidate = Path(f'/run/user/{os.getuid()}/gdm/Xauthority')
        if candidate.is_file():
            xauth = str(candidate)
    if xauth:
        env['XAUTHORITY'] = xauth
    return env


def _auto_login_url_allowed(url: str) -> bool:
    """Only ever auto-open the authenticated Partner/Google doc hosts.

    The launch is opt-in but runs unattended, so the URL must not be able to
    drift into arbitrary commands or phishing hosts: https only, host must be
    an exact or subdomain match of the cookie allowlist suffixes that this
    module already trusts for the docs session.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != 'https' or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == suffix.lstrip('.') or host.endswith(suffix) for suffix in COOKIE_HOST_SUFFIXES)


def launch_login_browser(url: str = DEFAULT_URL, *, reason: str = '', verbose: bool = False) -> bool:
    """Open `url` in the host desktop Firefox to restore the Google session.

    Sync jobs call this when Partner authentication fails: the browser has
    the saved account session and password manager, so recovery takes a few
    clicks without any credential handling in code. Off by default — callers
    must surface manual sign-in guidance in their error text; opt in with
    GMS_PARTNER_ANDROID_AUTO_LOGIN=1. Debounced via a marker file so
    repeated failures in one scan launch at most one window. The URL is
    constrained to https on the Android/Google doc hosts (_auto_login_url_allowed).
    """
    if not _auto_login_url_allowed(url):
        if verbose:
            print(f'auto-login launch rejected: URL outside trusted Partner hosts: {url}', file=sys.stderr)
        return False
    if not _auto_login_enabled():
        if verbose:
            print(f'auto-login launch disabled ({AUTO_LOGIN_ENV}=1 to enable)', file=sys.stderr)
        return False
    marker = _auto_login_marker_path()
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < AUTO_LOGIN_DEBOUNCE_SECONDS:
            if verbose:
                print('auto-login launch skipped: launched recently (debounce)', file=sys.stderr)
            return False
    except OSError:
        pass
    firefox = shutil.which('firefox')
    env = _auto_login_display_environment()
    if not firefox or env is None:
        if verbose:
            print('auto-login launch unavailable: firefox binary or X display missing', file=sys.stderr)
        return False
    try:
        # 审计过的豁免边界：url 已在函数入口经 _auto_login_url_allowed 白名单
        # 校验（https + COOKIE_HOST_SUFFIXES 精确/后缀匹配），taint 传播到此的
        # 只有操作员配置的 Partner 文档地址；argv 数组形式、无 shell。Semgrep
        # taint 模式无法把自定义白名单识别为 sanitizer，按仓库约定在 sink 行豁免。
        subprocess.Popen(
            [firefox, url],  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        if verbose:
            print(f'auto-login launch failed: {exc}', file=sys.stderr)
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f'{int(time.time())}\t{reason}\t{url}\n', encoding='utf-8')
    except OSError:
        pass
    if verbose:
        print(
            f'auto-login: opened {url} in host Firefox ({reason or "partner auth failure"}); '
            'sign in there to restore cookies, then re-run the sync',
            file=sys.stderr,
        )
    return True


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
