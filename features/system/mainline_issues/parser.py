#!/usr/bin/env python3
"""Sync Mainline known issues from Android Partner release notes."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urldefrag, urljoin

import requests
from lxml import html

from foundation import partner_android as fetch_partner_android


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

