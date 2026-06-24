#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import argparse
import sys
from pathlib import Path

import requests
from lxml import html

from foundation import partner_android as fetch_partner_android


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1

# How many recent months of Mainline PRELOAD notes to crawl per scan. Keeps the
# number of authenticated child-page fetches bounded (one per PRELOAD build).
MAINLINE_MONTH_DEPTH = 12
MAINLINE_INDEX_URL = 'https://docs.partner.android.com/mainline/release/release-notes?authuser=2'


from .models import *
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
