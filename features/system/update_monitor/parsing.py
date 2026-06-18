#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from lxml import html


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1


from .models import *


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def text_content(node: html.HtmlElement) -> str:
    return ' '.join(' '.join(node.xpath('.//text()')).split())


def direct_text(node: html.HtmlElement) -> str:
    return ' '.join(' '.join(node.xpath('./text()')).split())


def article_node(doc: html.HtmlElement) -> html.HtmlElement:
    articles = doc.xpath('//article[contains(@class, "devsite-article")]')
    return articles[0] if articles else doc


def clean_title(doc: html.HtmlElement) -> str:
    title = ' '.join(doc.xpath('//title/text()')).strip()
    return re.sub(r'\s+', ' ', title)


def stable_doc_hash(doc: html.HtmlElement) -> str:
    doc_copy = html.fromstring(html.tostring(doc))
    for node in doc_copy.xpath('//script|//style|//noscript'):
        node.drop_tree()
    return stable_hash(text_content(article_node(doc_copy)))


def normalize_key(*parts: str) -> str:
    raw = '|'.join((part or '').strip().lower() for part in parts)
    raw = re.sub(r'\s+', ' ', raw)
    return stable_hash(raw)[:32]


def heading_level(node: html.HtmlElement) -> int:
    return int(node.tag[1]) if re.fullmatch(r'h[1-6]', node.tag or '') else 0


def heading_nodes(article: html.HtmlElement, levels: tuple[int, ...] = (2, 3)) -> list[html.HtmlElement]:
    tags = ' or '.join(f'self::h{level}' for level in levels)
    return article.xpath(f'.//*[{tags}]')


def section_siblings(heading: html.HtmlElement) -> list[html.HtmlElement]:
    level = heading_level(heading)
    nodes: list[html.HtmlElement] = []
    for sibling in heading.itersiblings():
        sibling_level = heading_level(sibling)
        if sibling_level and sibling_level <= level:
            break
        nodes.append(sibling)
    return nodes


def nearest_previous_heading(node: html.HtmlElement, tag: str) -> str:
    previous = node.xpath(f'preceding-sibling::{tag}[1]')
    return text_content(previous[0]) if previous else ''


def node_links(node: html.HtmlElement, base_url: str) -> list[LinkInfo]:
    links: list[LinkInfo] = []
    for anchor in node.xpath('.//a[@href]'):
        text = text_content(anchor)
        href = anchor.get('href') or ''
        if not href:
            continue
        links.append(LinkInfo(text=text, url=urljoin(base_url, href)))
    return links


def node_links_json(node: html.HtmlElement, base_url: str) -> str:
    return json.dumps([asdict(link) for link in node_links(node, base_url)], ensure_ascii=False, sort_keys=True)


def cell_text(cell: html.HtmlElement) -> str:
    return text_content(cell)


def cell_first_link(cell: html.HtmlElement, base_url: str) -> LinkInfo:
    links = node_links(cell, base_url)
    return links[0] if links else LinkInfo(text='', url='')


def table_rows(table: html.HtmlElement, base_url: str) -> tuple[list[str], list[dict[str, Any]]]:
    rows = table.xpath('./thead/tr|./tbody/tr|./tr')
    if not rows:
        rows = table.xpath('.//tr')
    header_cells = rows[0].xpath('./th|./td') if rows else []
    headers = [cell_text(cell) for cell in header_cells]
    parsed_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows[1:], 1):
        cells = row.xpath('./td|./th')
        if not cells:
            continue
        values = [cell_text(cell) for cell in cells]
        links = [asdict(link) for cell in cells for link in node_links(cell, base_url)]
        parsed_rows.append({'row_index': row_index, 'values': values, 'links': links, 'cells': cells})
    return headers, parsed_rows


def infer_arch(text: str) -> str:
    lower = text.lower()
    if 'x86_64' in lower or 'x86 64' in lower:
        return 'x86_64'
    if re.search(r'(^|[^a-z0-9])x86([^a-z0-9]|$)', lower):
        return 'x86'
    if 'arm64' in lower:
        return 'arm64'
    if re.search(r'(^|[^a-z0-9])arm([^a-z0-9]|$)', lower):
        return 'arm'
    return ''


def is_arm_arch(arch: str) -> bool:
    return arch in ('arm', 'arm64')


def infer_suite_type(text: str, fallback: str = '') -> str:
    lower = text.lower()
    if 'cts verifier' in lower:
        return 'CTS Verifier'
    if 'cts media' in lower:
        return 'CTS Media'
    if 'compatibility test suite' in lower or re.search(r'\bcts\b', lower):
        return 'CTS'
    if re.search(r'\bvts\b', lower):
        return 'VTS'
    if 'gki' in lower:
        return 'GKI'
    if 'gsi' in lower:
        return 'GSI'
    if re.search(r'\bgts\b', lower):
        return 'GTS'
    return fallback


def android_major_version(*values: str) -> int | None:
    combined = ' '.join(value for value in values if value)
    patterns = (
        r'Android\s+(\d+)',
        r'\b(?:CTS|VTS|GTS)[-_ ]?(\d+)(?:[._ ]|$)',
        r'\bandroid-(?:cts|vts|gts)-(\d+)(?:[._-]|$)',
        r'\bgms-oem-[A-Z]+-(\d+)(?:[._-]|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def is_android_14_or_newer(*values: str) -> bool:
    major = android_major_version(*values)
    return bool(major is not None and major >= 14)


def gts_major_version(*values: str) -> int | None:
    combined = ' '.join(value for value in values if value)
    match = re.search(r'\bGTS[_\s-]+(\d+)', combined, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_gts_13_or_newer(*values: str) -> bool:
    major = gts_major_version(*values)
    return bool(major is not None and major >= 13)


def file_name_from_url_or_text(url: str, text: str) -> str:
    if text and ('.zip' in text or '.apk' in text or '.img' in text):
        return text.strip()
    tail = url.split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
    return tail if '.' in tail else text.strip()


def ci_build_id(url: str) -> str:
    match = re.search(r'/submitted/(\d+)/', url)
    return match.group(1) if match else ''


def normalized_vts_file_name(url: str, release_name: str, suite_type: str, arch: str) -> str:
    build_id = ci_build_id(url)
    if suite_type == 'VTS' and build_id:
        suffix = arch or 'arm64'
        return f'android-vts-{build_id}_{suffix}.zip'
    return file_name_from_url_or_text(url, release_name)


def meaningful_requirement_heading(title: str, level: int) -> bool:
    normalized = re.sub(r'\s+', ' ', title).strip()
    if not normalized:
        return False
    if level == 2:
        return True
    noisy_patterns = (
        r'^Requirement ID$',
        r'^Hide\s+',
        r'^DEVICE$',
        r'^CHIPSET-\d+$',
        r'^PRODUCT-\d+$',
        r'^SOFTWARE-\d+$',
        r'^BUILD-\d+$',
        r'^Table of contents$',
    )
    return not any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in noisy_patterns)


def focused_section_siblings(heading: html.HtmlElement) -> list[html.HtmlElement]:
    level = heading_level(heading)
    nodes: list[html.HtmlElement] = []
    for sibling in heading.itersiblings():
        sibling_level = heading_level(sibling)
        if sibling_level and sibling_level <= level:
            break
        if level == 2 and sibling_level and sibling_level > level:
            break
        nodes.append(sibling)
    return nodes


def requirement_ids(text: str) -> list[str]:
    ids = re.findall(r'\[([A-Z]+(?:-[A-Z]+)?-[0-9][A-Z0-9_.-]*)\]', text)
    return sorted(dict.fromkeys(ids))


def classify_requirement_versions(text: str, inherited_kind: str = '') -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    patterns = (
        (r'Start of requirements added in Android\s+(1[5-7])', 'added'),
        (r'Start of requirements changed in Android\s+(1[5-7])', 'changed'),
        (r'Start of Android\s+(1[5-7])\s+requirements', 'added'),
        (r'Android\s+(1[5-7])\s+QPR\d+', inherited_kind or 'specific'),
        (r'Android\s+(1[5-7])\s+or higher', inherited_kind or 'specific'),
        (r'Android\s+(1[5-7])\s+and higher', inherited_kind or 'specific'),
        (r'Android\s+(1[5-7])\s+and\s+1[5-9]', inherited_kind or 'specific'),
        (r'launching with Android\s+(1[5-7])', inherited_kind or 'specific'),
        (r'running Android\s+(1[5-7])', inherited_kind or 'specific'),
        (r'introduced in Android\s+(1[5-7])', inherited_kind or 'added'),
        (r'updated in Android\s+(1[5-7])', inherited_kind or 'changed'),
    )
    for pattern, kind in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            version = f'Android {match.group(1)}'
            found.append((version, kind))
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def classify_marker_kind(text: str) -> str:
    if re.search(r'Start of requirements added in Android|Start of Android\s+1[5-7]\s+requirements', text, re.I):
        return 'added'
    if re.search(r'Start of requirements changed in Android', text, re.I):
        return 'changed'
    return ''


def requirement_candidate_nodes(siblings: list[html.HtmlElement]) -> list[html.HtmlElement]:
    candidates: list[html.HtmlElement] = []
    for sibling in siblings:
        if sibling.tag in ('p', 'li', 'td'):
            candidates.append(sibling)
        candidates.extend(sibling.xpath('.//*[self::p or self::li or self::td]'))
    return candidates


def nearest_requirement_path(node: html.HtmlElement, fallback_path: str) -> str:
    headings = node.xpath('preceding::*[self::h2 or self::h3 or self::h4]')
    heading = None
    for candidate in reversed(headings):
        if meaningful_requirement_heading(text_content(candidate), heading_level(candidate)):
            heading = candidate
            break
    if heading is None:
        return fallback_path
    heading_text = text_content(heading)
    if heading.tag == 'h2':
        return heading_text
    h2 = heading.xpath('preceding::h2[1]')
    h2_text = text_content(h2[0]) if h2 else ''
    if heading.tag == 'h3':
        return f'{h2_text} / {heading_text}'.strip(' /')
    h3_text = ''
    for candidate_h3 in reversed(heading.xpath('preceding::h3')):
        if meaningful_requirement_heading(text_content(candidate_h3), heading_level(candidate_h3)):
            h3_text = text_content(candidate_h3)
            break
    return ' / '.join(part for part in (h2_text, h3_text, heading_text) if part)
