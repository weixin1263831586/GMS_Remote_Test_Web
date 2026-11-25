#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from lxml import etree, html

from foundation import partner_android as fetch_partner_android

from .models import FetchedDocument, SourceConfig
from .parsing import clean_title, stable_doc_hash


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
        url='https://docs.partner.android.com/',
        cookie_file=args.cookie_file,
        cookie_header=args.cookie_header,
        browser=args.browser,
        verbose=args.verbose,
    )
    if args.verbose and not loaded:
        print('warning: no browser cookies loaded; Partner pages may fail authentication', file=sys.stderr)
    return session


def fetch_source(session: requests.Session, source: SourceConfig, timeout: float) -> FetchedDocument:
    response = session.get(source.url, timeout=timeout, allow_redirects=True)
    if source.auth_required and fetch_partner_android.is_auth_failure(response.status_code, str(response.url)):
        # 认证失效默认不再自动弹桌面浏览器（GMS_PARTNER_ANDROID_AUTO_LOGIN=1
        # 可恢复旧行为），报错里直接给出手动登录指引。
        if fetch_partner_android.launch_login_browser(
                source.url, reason=f'{source.name} HTTP {response.status_code}'):
            opened_hint = '已尝试在主机桌面 Firefox 打开该页'
        else:
            opened_hint = '请在本机 Firefox 登录 docs.partner.android.com 的 Google 账号（无需保持页面打开）'
        raise RuntimeError(
            f'{source.name} 访问失败 (HTTP {response.status_code})：'
            'Android Partner 登录可能已失效、当前账号无权限或页面地址已调整；'
            f'扫描读取的是本机浏览器的登录 Cookie，{opened_hint}，完成登录后重试'
        )
    response.raise_for_status()
    doc = html.fromstring(response.content)
    return FetchedDocument(
        source=source,
        doc=doc,
        title=clean_title(doc),
        content_hash=stable_doc_hash(doc),
        status_code=response.status_code,
        final_url=response.url,
    )


def fetch_html(session: requests.Session | None, url: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Fetch a single authenticated child page defensively.

    Returns ``(status_code, final_url, text)``. Does not raise on HTTP errors
    because authenticated Partner pages can transiently 404 (e.g. before the
    browser session is fully signed in); callers decide what to do with a 404.
    """
    if session is None:
        return 0, url, ''
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
    except Exception:
        return 0, url, ''
    return response.status_code, response.url, getattr(response, 'text', '') or ''


def build_train_url(build_id: str) -> str:
    """Map a Mainline PRELOAD partner-zip build number to its CI build page."""
    return f'https://ci.android.com/builds/train/{build_id}/train_build/latest?authuser=2'


def recent_month_cutoff(depth: int, *, now_year: int | None = None, now_month: int | None = None) -> tuple[int, int]:
    """Return the ``(year, month)`` cutoff for the last ``depth`` months.

    ``depth`` months back from ``now`` inclusive — e.g. depth=12 starting in
    2026-06 yields cutoff (2025, 7). Pure arithmetic, no dateutil dependency.
    Used only as an import-time-free helper; the actual "now" is injected at
    call sites so the function stays deterministic for tests.
    """
    import datetime as _dt

    if now_year is None or now_month is None:
        today = _dt.date.today()
        now_year, now_month = today.year, today.month
    total = now_year * 12 + (now_month - 1) - max(depth - 1, 0)
    return total // 12, total % 12 + 1


HOSTS = frozenset(('docs.partner.android.com', 'source.android.com'))
RETRYABLE = {429, 500, 502, 503, 504}


def canonical_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in HOSTS or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError('untrusted scan URL')
    query = parse_qsl(parts.query, keep_blank_values=True)
    if parts.hostname == 'docs.partner.android.com' and parts.path.startswith('/mainline/release/release-notes'):
        # PRELOAD and Known Issues must access the same Google account.
        query = [(k, v) for k, v in query if k != 'authuser'] + [('authuser', '2')]
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), urlencode(sorted(query)), ''))


class BatchFetcher:
    def __init__(self, session, *, retries=2, on_document=None):
        self.session = session
        self.retries = retries
        self.on_document = on_document
        self.cache = {}
        self.documents = {}
        self.cache_evidence = {}

    def get(self, url, **kwargs):
        key = canonical_url(url)
        if key in self.cache:
            response = self.cache[key]
            self.documents[key] = self.cache_evidence[key]
            return response
        evidence = (0, '', 'failed', 0)
        response = None
        for attempt in range(1, self.retries + 2):
            response = None
            try:
                response = self.session.get(key, **kwargs)
                code = response.status_code
                evidence = (code, '', 'failed', attempt)
                if code == 200:
                    try:
                        final = canonical_url(response.url)
                        doc = html.fromstring(response.content)
                        a,b=urlsplit(key),urlsplit(final)
                        same=(a.hostname==b.hostname and a.path==b.path)
                        from .parsers import bulletin_month_identity
                        month=bulletin_month_identity(key)
                        if month and month==bulletin_month_identity(final):
                            same=True
                        if same and doc.xpath('//article[contains(@class,"devsite-article")]'):
                            evidence = (code, stable_doc_hash(doc), 'ready', attempt)
                    except (ValueError, TypeError, etree.ParserError):
                        pass
                if evidence[2] == 'ready' or code not in RETRYABLE:
                    break
            except requests.RequestException:
                evidence = (0, '', 'failed', attempt)
                if attempt > self.retries:
                    self.documents[key] = evidence
                    if self.on_document:
                        self.on_document(key,evidence)
                    raise
            if attempt <= self.retries:
                time.sleep(min(attempt, 2))
        self.documents[key] = evidence
        if self.on_document:
            self.on_document(key,evidence)
        if evidence[2] == 'ready':
            self.cache[key] = response
            self.cache_evidence[key] = evidence
        if response is None:
            raise requests.ConnectionError('scan request unavailable')
        return response

    def begin_source(self):
        self.documents = {}
        self.cache = {}
        self.cache_evidence = {}
