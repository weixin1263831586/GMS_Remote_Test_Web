#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import argparse
import sys
from pathlib import Path

import requests
from lxml import html


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1


from .models import *


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
