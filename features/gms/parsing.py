#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from lxml import html

from .models import LinkInfo


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def text_content(node: html.HtmlElement) -> str:
    return ' '.join(' '.join(node.xpath('.//text()')).split())


def matched_excerpt(text: str, terms: list[str], *, limit: int = 4000) -> str:
    """Bound prompt content while retaining matches beyond the initial window."""
    if len(text) <= limit:
        return text
    lower = text.lower()
    ranked = sorted({(not bool(re.fullmatch(r'CVE-\d{4}-\d{4,7}', term, re.I)), lower.find(term.lower()))
                     for term in terms if term and term.lower() in lower})
    # Prefer CVE matches when many other identifiers appear earlier in a long
    # section, and bound the number of windows before slicing the final text.
    positions = sorted(
        list(dict.fromkeys(position for _priority, position in ranked))[:max(1, min(4, limit // 900))],
    )
    if not positions:
        return text[:limit]
    windows = []
    for position in positions:
        start, end = max(0, position - 250), min(len(text), position + 650)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(end, windows[-1][1]))
        else:
            windows.append((start, end))
    return '\n…\n'.join(text[start:end] for start, end in windows)[:limit]


def reference_term_matches(text: str | None, term: str) -> bool:
    """CVE identifiers are exact tokens; other search terms are literal text."""
    if re.fullmatch(r'CVE-\d{4}-\d{4,7}', term, re.I):
        return bool(re.search(rf'(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])', text or '', re.I))
    return term.casefold() in (text or '').casefold()


def article_node(doc: html.HtmlElement) -> html.HtmlElement:
    articles = doc.xpath('//article[contains(@class, "devsite-article")]')
    return articles[0] if articles else doc


def clean_title(doc: html.HtmlElement) -> str:
    title = ' '.join(doc.xpath('//title/text()')).strip()
    return re.sub(r'\s+', ' ', title)


def _document_without_scripts(doc: html.HtmlElement) -> html.HtmlElement:
    doc_copy = html.fromstring(html.tostring(doc))
    for node in doc_copy.xpath('//script|//style|//noscript'):
        node.drop_tree()
    return doc_copy


def stable_doc_hash(doc: html.HtmlElement) -> str:
    doc_copy = _document_without_scripts(doc)
    article = article_node(doc_copy)
    return stable_hash({
        'text': text_content(article),
        'links': article.xpath('.//a/@href'),
        'structure': [(node.tag, node.get('rowspan'), node.get('colspan'))
                      for node in article.xpath('.//h2|.//h3|.//h4|.//h5|.//h6|.//td|.//th')],
        'release_links': sorted(set(doc_copy.xpath('//a[contains(@href, "/release-notes/")]/@href'))),
    })


def mainline_doc_hash(doc: html.HtmlElement) -> str:
    """Preserve the historical Mainline page fingerprint encoding."""
    target = article_node(_document_without_scripts(doc))
    stable_text = repr((text_content(target), target.xpath('.//a/@href'),
                        [(node.tag, node.get('rowspan'), node.get('colspan'))
                         for node in target.xpath('.//td|.//th|.//h2|.//h3|.//h4')]))
    return stable_hash(stable_text)


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


def cell_text(cell: html.HtmlElement) -> str:
    return text_content(cell)


def cell_first_link(cell: html.HtmlElement, base_url: str) -> LinkInfo:
    links = node_links(cell, base_url)
    return links[0] if links else LinkInfo(text='', url='')


def table_rows(table: html.HtmlElement, base_url: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Expand HTML row/column spans into logical cells before interpreting headers."""
    rows = table.xpath('./thead/tr|./tbody/tr|./tfoot/tr|./tr')
    if not rows:
        return [], []
    pending: dict[int, tuple[html.HtmlElement, int]] = {}
    expanded = []

    def span(cell, name):
        try:
            return max(1, min(256, int(cell.get(name, '1'))))
        except ValueError:
            return 1

    for row in rows:
        grid = {col: cell for col, (cell, _) in pending.items()}
        pending = {col: (cell, left - 1) for col, (cell, left) in pending.items() if left > 1}
        column = 0
        for cell in row.xpath('./th|./td'):
            width = span(cell, 'colspan')
            while any(col in grid for col in range(column, column + width)):
                column += 1
            height = span(cell, 'rowspan')
            for col in range(column, column + width):
                grid[col] = cell
                if height > 1:
                    pending[col] = (cell, height - 1)
            column += width
        expanded.append([grid.get(col) for col in range(max(grid, default=-1) + 1)])
    headers = [cell_text(cell) if cell is not None else '' for cell in expanded[0]]
    parsed_rows: list[dict[str, Any]] = []
    for row_index, cells in enumerate(expanded[1:], 1):
        if not cells or any(cell is None for cell in cells):
            continue
        values = [cell_text(cell) for cell in cells]
        links = [asdict(link) for cell in dict.fromkeys(cells) for link in node_links(cell, base_url)]
        parsed_rows.append({'row_index': row_index, 'values': values, 'links': links, 'cells': cells})
    return headers, parsed_rows


def infer_arch(text: str) -> str:
    lower = text.lower()
    if 'x86_64' in lower or 'x86 64' in lower:
        return 'x86_64'
    if re.search(r'(^|[^a-z0-9])x86([^a-z0-9]|$)', lower):
        return 'x86'
    if re.search(r'(?<![a-z0-9])(?:arm[ _-]?64|aarch64)(?![a-z0-9])', lower):
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
        if sibling_level:
            break
        nodes.append(sibling)
    return nodes


def requirement_ids(text: str) -> list[str]:
    ids = re.findall(r'\[([A-Z]+(?:-[A-Z]+)?-[0-9][A-Z0-9_.-]*)\]', text)
    return sorted(dict.fromkeys(ids))


def classify_requirement_versions(text: str, inherited_kind: str = '') -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    version_pattern = r'(\d{2}(?:\s+QPR\d+)?)'
    patterns = (
        (r'Start of requirements added in Android\s+' + version_pattern, 'added'),
        (r'Start of requirements changed in Android\s+' + version_pattern, 'changed'),
        (r'Start of Android\s+' + version_pattern + r'\s+requirements', 'added'),
    )
    for pattern, kind in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            version = 'Android ' + re.sub(r'\s+', ' ', match.group(1).upper())
            found.append((version, kind))
    # An explicit local marker takes precedence over inherited classification.
    # Otherwise a changed marker in a section containing an added marker
    # produces two contradictory records for the same Android version.
    explicit_versions = {version for version, _ in found}
    for match in re.finditer(r'Android\s+' + version_pattern, text, flags=re.IGNORECASE):
        version = 'Android ' + re.sub(r'\s+', ' ', match.group(1).upper())
        if version not in explicit_versions:
            found.append((version, inherited_kind or 'specific'))
    for match in re.finditer(r'Android\s+\d{2}(?:\s+QPR\d+)?((?:\s*(?:,|and)\s*(?:Android\s+)?\d{2}(?:\s+QPR\d+)?)+)', text, flags=re.I):
        for token in re.findall(r'\d{2}(?:\s+QPR\d+)?', match.group(1), flags=re.I):
            version = 'Android ' + re.sub(r'\s+', ' ', token.upper())
            if version not in explicit_versions:
                found.append((version, inherited_kind or 'specific'))
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def classify_marker_kind(text: str) -> str:
    if re.search(r'Start of requirements added in Android|Start of Android\s+\d{2}(?:\s+QPR\d+)?\s+requirements', text, re.I):
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
    headings = node.xpath('preceding::*[self::h2 or self::h3 or self::h4 or self::h5 or self::h6]')
    heading = None
    for candidate in reversed(headings):
        if meaningful_requirement_heading(text_content(candidate), heading_level(candidate)):
            heading = candidate
            break
    if heading is None:
        return fallback_path
    chain = [text_content(heading)]
    level = heading_level(heading)
    for previous in reversed(headings[:headings.index(heading)]):
        previous_level = heading_level(previous)
        if previous_level < level and meaningful_requirement_heading(text_content(previous), previous_level):
            chain.append(text_content(previous))
            level = previous_level
    return ' / '.join(reversed(chain))


# 月份目录允许缩写和全称；从同一份月份名称生成双向索引。
_MONTH_NAMES = (
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
)
MONTH_LOOKUP = {
    key: (idx, label)
    for idx, label in enumerate(_MONTH_NAMES, 1)
    for key in (label[:3].lower(), label.lower())
}
MONTH_INDEX_TO_KEY = {idx: label[:3].lower() for idx, label in enumerate(_MONTH_NAMES, 1)}
