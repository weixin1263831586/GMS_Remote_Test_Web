#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from lxml import html

try:
    from . import fetch_partner_android
except ImportError:  # pragma: no cover - supports direct script execution from modules/
    import fetch_partner_android


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    url: str
    category: str
    parser: str
    auth_required: bool = False


@dataclass(frozen=True)
class FetchedDocument:
    source: SourceConfig
    doc: html.HtmlElement
    title: str
    content_hash: str
    status_code: int
    final_url: str


@dataclass(frozen=True)
class LinkInfo:
    text: str
    url: str


@dataclass(frozen=True)
class ArtifactRecord:
    source_key: str
    item_key: str
    suite_type: str
    android_version: str
    release_name: str
    artifact_kind: str
    arch: str
    file_name: str
    download_url: str
    release_notes_url: str
    user_guide_url: str
    ci_build_id: str
    target_platform: str
    description: str
    section_path: str
    content_hash: str


@dataclass(frozen=True)
class GmsPackageRecord:
    source_key: str
    item_key: str
    section: str
    android_version: str
    release_notes_url: str
    file_name: str
    download_url: str
    required_from: str
    partner_gerrit_tag: str
    partner_gerrit_url: str
    description: str
    content_hash: str


@dataclass(frozen=True)
class RequirementSectionRecord:
    source_key: str
    section_key: str
    level: int
    number: str
    title: str
    path: str
    text_excerpt: str
    table_count: int
    link_count: int
    content_hash: str


@dataclass(frozen=True)
class RequirementTableRowRecord:
    source_key: str
    row_key: str
    section_key: str
    section_title: str
    table_index: int
    row_index: int
    headers_json: str
    values_json: str
    row_text: str
    content_hash: str


@dataclass(frozen=True)
class RequirementVersionTagRecord:
    source_key: str
    tag_key: str
    android_version: str
    change_kind: str
    section_key: str
    section_title: str
    requirement_ids: str
    text_excerpt: str
    content_hash: str


@dataclass
class ParsedSource:
    artifacts: list[ArtifactRecord] = None  # type: ignore[assignment]
    gms_packages: list[GmsPackageRecord] = None  # type: ignore[assignment]
    requirement_sections: list[RequirementSectionRecord] = None  # type: ignore[assignment]
    requirement_table_rows: list[RequirementTableRowRecord] = None  # type: ignore[assignment]
    requirement_version_tags: list[RequirementVersionTagRecord] = None  # type: ignore[assignment]

    def __post_init__(self):
        for field_name in ('artifacts', 'gms_packages', 'requirement_sections', 'requirement_table_rows', 'requirement_version_tags'):
            if getattr(self, field_name) is None:
                object.__setattr__(self, field_name, [])


SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        key='cts_downloads',
        name='CTS Downloads',
        url='https://source.android.com/docs/compatibility/cts/downloads',
        category='test_suite',
        parser='cts_downloads',
    ),
    SourceConfig(
        key='vts_downloads',
        name='VTS, GSIs, and GKIs',
        url='https://docs.partner.android.com/gms/testing/vts',
        category='test_suite',
        parser='vts_downloads',
        auth_required=True,
    ),
    SourceConfig(
        key='gts_downloads',
        name='GTS Downloads',
        url='https://docs.partner.android.com/gms/testing/gts',
        category='test_suite',
        parser='gts_downloads',
        auth_required=True,
    ),
    SourceConfig(
        key='gms_downloads',
        name='GMS Downloads',
        url='https://docs.partner.android.com/gms/building/integrating/gms-download',
        category='gms_package',
        parser='gms_downloads',
        auth_required=True,
    ),
    SourceConfig(
        key='gms_requirements',
        name='GMS Certification Requirements',
        url='https://docs.partner.android.com/gms/policies/domains/reqs',
        category='certification_requirement',
        parser='gms_requirements',
        auth_required=True,
    ),
)


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


def parse_cts_downloads(fetched: FetchedDocument) -> ParsedSource:
    article = article_node(fetched.doc)
    artifacts: list[ArtifactRecord] = []
    for heading in article.xpath('.//h2'):
        android_version = text_content(heading)
        if not is_android_14_or_newer(android_version):
            continue
        for sibling in section_siblings(heading):
            for link in node_links(sibling, fetched.final_url):
                release_name = link.text
                suite_type = infer_suite_type(release_name, 'CTS')
                arch = infer_arch(release_name)
                if arch and not is_arm_arch(arch):
                    continue
                file_name = file_name_from_url_or_text(link.url, release_name)
                payload = {
                    'source_key': fetched.source.key,
                    'android_version': android_version,
                    'release_name': release_name,
                    'download_url': link.url,
                }
                artifacts.append(
                    ArtifactRecord(
                        source_key=fetched.source.key,
                        item_key=normalize_key(fetched.source.key, android_version, release_name, link.url),
                        suite_type=suite_type,
                        android_version=android_version,
                        release_name=release_name,
                        artifact_kind='download',
                        arch=arch,
                        file_name=file_name,
                        download_url=link.url,
                        release_notes_url='',
                        user_guide_url='',
                        ci_build_id='',
                        target_platform='',
                        description=release_name,
                        section_path=android_version,
                        content_hash=stable_hash(payload),
                    )
                )
    return ParsedSource(artifacts=artifacts)


def parse_vts_downloads(fetched: FetchedDocument) -> ParsedSource:
    article = article_node(fetched.doc)
    artifacts: list[ArtifactRecord] = []
    for kind_heading in article.xpath('.//h3'):
        current_kind = text_content(kind_heading)
        version_headings = kind_heading.xpath('preceding::h2[1]')
        if not version_headings:
            continue
        android_version = text_content(version_headings[0])
        if not is_android_14_or_newer(android_version):
            continue
        kind_lower = current_kind.lower()
        if 'x86' in kind_lower or 'gki' in kind_lower:
            continue
        for sibling in section_siblings(kind_heading):
            for link in node_links(sibling, fetched.final_url):
                if not link.text or 'dashboard' in link.text.lower():
                    continue
                release_name = link.text
                suite_type = infer_suite_type(f'{current_kind} {release_name}', 'VTS')
                arch = infer_arch(f'{current_kind} {release_name} {link.url}')
                if arch and not is_arm_arch(arch):
                    continue
                if suite_type == 'GSI' and arch != 'arm64':
                    continue
                payload = {
                    'source_key': fetched.source.key,
                    'android_version': android_version,
                    'artifact_kind': current_kind,
                    'release_name': release_name,
                    'download_url': link.url,
                }
                artifacts.append(
                    ArtifactRecord(
                        source_key=fetched.source.key,
                        item_key=normalize_key(fetched.source.key, android_version, current_kind, release_name, link.url),
                        suite_type=suite_type,
                        android_version=android_version,
                        release_name=release_name,
                        artifact_kind=current_kind or 'download',
                        arch=arch,
                        file_name=normalized_vts_file_name(link.url, release_name, suite_type, arch),
                        download_url=link.url,
                        release_notes_url='',
                        user_guide_url='',
                        ci_build_id=ci_build_id(link.url),
                        target_platform='',
                        description=f'{android_version} {current_kind}'.strip(),
                        section_path=f'{android_version} / {current_kind}'.strip(' /'),
                        content_hash=stable_hash(payload),
                    )
                )
    return ParsedSource(artifacts=artifacts)


def parse_gts_downloads(fetched: FetchedDocument) -> ParsedSource:
    article = article_node(fetched.doc)
    artifacts: list[ArtifactRecord] = []
    for table in article.xpath('.//table'):
        headers, rows = table_rows(table, fetched.final_url)
        normalized_headers = [header.lower() for header in headers]
        if 'file' not in normalized_headers:
            continue
        for parsed in rows:
            values = parsed['values']
            cells = parsed['cells']
            by_header = {headers[i]: values[i] for i in range(min(len(headers), len(values)))}
            file_idx = normalized_headers.index('file')
            file_link = cell_first_link(cells[file_idx], fetched.final_url) if file_idx < len(cells) else LinkInfo('', '')
            guide_link = LinkInfo('', '')
            notes_link = LinkInfo('', '')
            if 'user guide' in normalized_headers:
                idx = normalized_headers.index('user guide')
                guide_link = cell_first_link(cells[idx], fetched.final_url) if idx < len(cells) else guide_link
            if 'release notes' in normalized_headers:
                idx = normalized_headers.index('release notes')
                notes_link = cell_first_link(cells[idx], fetched.final_url) if idx < len(cells) else notes_link
            release_name = file_link.text or by_header.get('File', '')
            if not is_gts_13_or_newer(
                by_header.get('Target platform', ''),
                by_header.get('Description', ''),
                release_name,
            ):
                continue
            payload = {
                'source_key': fetched.source.key,
                'description': by_header.get('Description', ''),
                'target_platform': by_header.get('Target platform', ''),
                'file': release_name,
                'download_url': file_link.url,
                'release_notes_url': notes_link.url,
            }
            artifacts.append(
                ArtifactRecord(
                    source_key=fetched.source.key,
                    item_key=normalize_key(fetched.source.key, release_name, file_link.url),
                    suite_type='GTS',
                    android_version='',
                    release_name=release_name,
                    artifact_kind='download',
                    arch=infer_arch(release_name),
                    file_name=file_name_from_url_or_text(file_link.url, release_name),
                    download_url=file_link.url,
                    release_notes_url=notes_link.url,
                    user_guide_url=guide_link.url,
                    ci_build_id='',
                    target_platform=by_header.get('Target platform', ''),
                    description=by_header.get('Description', ''),
                    section_path='Download GTS',
                    content_hash=stable_hash(payload),
                )
            )
    return ParsedSource(artifacts=artifacts)


def parse_gms_downloads(fetched: FetchedDocument) -> ParsedSource:
    article = article_node(fetched.doc)
    packages: list[GmsPackageRecord] = []
    for table in article.xpath('.//table'):
        section = nearest_previous_heading(table, 'h2')
        headers, rows = table_rows(table, fetched.final_url)
        lower_headers = [header.lower() for header in headers]
        if 'file' not in lower_headers:
            continue
        last_android_version = ''
        for parsed in rows:
            values = parsed['values']
            cells = parsed['cells']
            by_header = {headers[i]: values[i] for i in range(min(len(headers), len(values)))}
            android_version = by_header.get('Android version', '')
            file_idx = lower_headers.index('file')
            release_notes_idx = lower_headers.index('release notes') if 'release notes' in lower_headers else -1
            tag_idx = lower_headers.index('partner gerrit tag') if 'partner gerrit tag' in lower_headers else -1
            required_idx = (
                lower_headers.index('required for new ir builds seeking approvals from')
                if 'required for new ir builds seeking approvals from' in lower_headers
                else -1
            )

            # Some GMS package tables use rowspans for the Android version column.
            # Subsequent rows omit that first cell, so align cells from the right
            # and carry the last explicit Android version forward.
            if android_version.startswith('Android'):
                last_android_version = android_version
            elif lower_headers and lower_headers[0] == 'android version' and last_android_version:
                missing_cells = len(headers) - len(values)
                if missing_cells > 0:
                    android_version = last_android_version
                    file_idx = max(0, file_idx - missing_cells)
                    release_notes_idx = release_notes_idx - missing_cells if release_notes_idx >= 0 else -1
                    tag_idx = tag_idx - missing_cells if tag_idx >= 0 else -1
                    required_idx = required_idx - missing_cells if required_idx >= 0 else -1

            file_link = cell_first_link(cells[file_idx], fetched.final_url) if 0 <= file_idx < len(cells) else LinkInfo('', '')
            release_notes_link = LinkInfo('', '')
            tag_link = LinkInfo('', '')
            if 0 <= release_notes_idx < len(cells):
                release_notes_link = cell_first_link(cells[release_notes_idx], fetched.final_url)
            if 0 <= tag_idx < len(cells):
                tag_link = cell_first_link(cells[tag_idx], fetched.final_url)
            file_name = file_link.text or (cell_text(cells[file_idx]) if 0 <= file_idx < len(cells) else by_header.get('File', ''))
            description = by_header.get('Description', '')
            if not is_android_14_or_newer(android_version, description, file_name):
                continue
            required_from = cell_text(cells[required_idx]) if 0 <= required_idx < len(cells) else by_header.get('Required for new IR builds seeking approvals from', '')
            payload = {
                'source_key': fetched.source.key,
                'section': section,
                'android_version': android_version,
                'description': description,
                'file_name': file_name,
                'download_url': file_link.url,
                'required_from': required_from,
                'partner_gerrit_tag': tag_link.text or (cell_text(cells[tag_idx]) if 0 <= tag_idx < len(cells) else by_header.get('Partner Gerrit tag', '')),
            }
            packages.append(
                GmsPackageRecord(
                    source_key=fetched.source.key,
                    item_key=normalize_key(fetched.source.key, section, android_version, description, file_name, file_link.url),
                    section=section,
                    android_version=android_version,
                    release_notes_url=release_notes_link.url,
                    file_name=file_name,
                    download_url=file_link.url,
                    required_from=required_from,
                    partner_gerrit_tag=tag_link.text or (cell_text(cells[tag_idx]) if 0 <= tag_idx < len(cells) else by_header.get('Partner Gerrit tag', '')),
                    partner_gerrit_url=tag_link.url,
                    description=description,
                    content_hash=stable_hash(payload),
                )
            )
    return ParsedSource(gms_packages=packages)


def section_key_from_heading(heading: html.HtmlElement, path: str) -> tuple[str, str, str]:
    title = text_content(heading)
    match = re.match(r'^(\d+(?:\.\d+)*)\.?\s*(.*)$', title)
    number = match.group(1) if match else ''
    clean = match.group(2).strip() if match else title
    return normalize_key(path or title), number, clean


def parse_gms_requirements(fetched: FetchedDocument) -> ParsedSource:
    article = article_node(fetched.doc)
    sections: list[RequirementSectionRecord] = []
    table_rows_out: list[RequirementTableRowRecord] = []
    version_tags: list[RequirementVersionTagRecord] = []
    version_tag_keys: set[str] = set()
    headings = heading_nodes(article, levels=(2, 3))
    h2_path = ''
    table_index = 0
    for heading in headings:
        level = heading_level(heading)
        title = text_content(heading)
        if not meaningful_requirement_heading(title, level):
            continue
        if level == 2:
            h2_path = title
        path = h2_path if level == 2 else f'{h2_path} / {title}'.strip(' /')
        section_key, number, clean = section_key_from_heading(heading, path)
        siblings = focused_section_siblings(heading)
        section_text = ' '.join([title, *[text_content(sibling) for sibling in siblings]])
        section_hash = stable_hash(section_text)
        tables = [table for sibling in siblings for table in sibling.xpath('.//table')]
        links = [link for sibling in siblings for link in node_links(sibling, fetched.final_url)]
        sections.append(
            RequirementSectionRecord(
                source_key=fetched.source.key,
                section_key=section_key,
                level=level,
                number=number,
                title=clean,
                path=path,
                text_excerpt=section_text[:1000],
                table_count=len(tables),
                link_count=len(links),
                content_hash=section_hash,
            )
        )

        marker_kind = classify_marker_kind(section_text)
        for candidate in requirement_candidate_nodes(siblings):
            candidate_text = text_content(candidate)
            if not candidate_text:
                continue
            ids = requirement_ids(candidate_text)
            if not ids and not re.search(r'Android\s+1[5-7]|Start of requirements', candidate_text, re.I):
                continue
            classifications = classify_requirement_versions(candidate_text, marker_kind)
            if not classifications:
                continue
            ids_text = ', '.join(ids)
            candidate_path = nearest_requirement_path(candidate, path)
            candidate_section_key = normalize_key(candidate_path)
            for android_version, change_kind in classifications:
                tag_key = normalize_key(fetched.source.key, android_version, change_kind, candidate_section_key, ids_text, candidate_text[:300])
                if tag_key in version_tag_keys:
                    continue
                version_tag_keys.add(tag_key)
                payload = {
                    'source_key': fetched.source.key,
                    'android_version': android_version,
                    'change_kind': change_kind,
                    'section_key': candidate_section_key,
                    'requirement_ids': ids_text,
                    'text': candidate_text,
                }
                version_tags.append(
                    RequirementVersionTagRecord(
                        source_key=fetched.source.key,
                        tag_key=tag_key,
                        android_version=android_version,
                        change_kind=change_kind,
                        section_key=candidate_section_key,
                        section_title=candidate_path,
                        requirement_ids=ids_text,
                        text_excerpt=candidate_text[:1200],
                        content_hash=stable_hash(payload),
                    )
                )

        for table in tables:
            table_index += 1
            headers, rows = table_rows(table, fetched.final_url)
            for parsed in rows:
                row_index = int(parsed['row_index'])
                values = parsed['values']
                row_text = ' | '.join(values)
                values_json = json.dumps(values, ensure_ascii=False)
                headers_json = json.dumps(headers, ensure_ascii=False)
                row_hash = stable_hash({'headers': headers, 'values': values})
                table_rows_out.append(
                    RequirementTableRowRecord(
                        source_key=fetched.source.key,
                        row_key=normalize_key(fetched.source.key, section_key, str(table_index), str(row_index), row_text),
                        section_key=section_key,
                        section_title=path,
                        table_index=table_index,
                        row_index=row_index,
                        headers_json=headers_json,
                        values_json=values_json,
                        row_text=row_text[:2000],
                        content_hash=row_hash,
                    )
                )
    return ParsedSource(
        requirement_sections=sections,
        requirement_table_rows=table_rows_out,
        requirement_version_tags=version_tags,
    )


PARSERS = {
    'cts_downloads': parse_cts_downloads,
    'vts_downloads': parse_vts_downloads,
    'gts_downloads': parse_gts_downloads,
    'gms_downloads': parse_gms_downloads,
    'gms_requirements': parse_gms_requirements,
}


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


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            source_filter TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            sources_scanned INTEGER NOT NULL,
            sources_skipped INTEGER NOT NULL,
            artifacts_total INTEGER NOT NULL,
            packages_total INTEGER NOT NULL,
            requirement_sections_total INTEGER NOT NULL,
            requirement_table_rows_total INTEGER NOT NULL,
            changes_total INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_sources (
            source_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            final_url TEXT NOT NULL,
            category TEXT NOT NULL,
            parser TEXT NOT NULL,
            auth_required INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            title TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_artifacts (
            source_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            suite_type TEXT NOT NULL,
            android_version TEXT NOT NULL,
            release_name TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            arch TEXT NOT NULL,
            file_name TEXT NOT NULL,
            download_url TEXT NOT NULL,
            release_notes_url TEXT NOT NULL,
            user_guide_url TEXT NOT NULL,
            ci_build_id TEXT NOT NULL,
            target_platform TEXT NOT NULL,
            description TEXT NOT NULL,
            section_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, item_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_packages (
            source_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            section TEXT NOT NULL,
            android_version TEXT NOT NULL,
            release_notes_url TEXT NOT NULL,
            file_name TEXT NOT NULL,
            download_url TEXT NOT NULL,
            required_from TEXT NOT NULL,
            partner_gerrit_tag TEXT NOT NULL,
            partner_gerrit_url TEXT NOT NULL,
            description TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, item_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_requirement_sections (
            source_key TEXT NOT NULL,
            section_key TEXT NOT NULL,
            level INTEGER NOT NULL,
            number TEXT NOT NULL,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            text_excerpt TEXT NOT NULL,
            table_count INTEGER NOT NULL,
            link_count INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, section_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_requirement_table_rows (
            source_key TEXT NOT NULL,
            row_key TEXT NOT NULL,
            section_key TEXT NOT NULL,
            section_title TEXT NOT NULL,
            table_index INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            headers_json TEXT NOT NULL,
            values_json TEXT NOT NULL,
            row_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, row_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_requirement_version_tags (
            source_key TEXT NOT NULL,
            tag_key TEXT NOT NULL,
            android_version TEXT NOT NULL,
            change_kind TEXT NOT NULL,
            section_key TEXT NOT NULL,
            section_title TEXT NOT NULL,
            requirement_ids TEXT NOT NULL,
            text_excerpt TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, tag_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gms_update_change_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            change_type TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            detected_at TEXT NOT NULL
        )
        """
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_artifacts_suite ON gms_update_artifacts(suite_type, android_version)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_packages_version ON gms_update_packages(android_version, section)')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_gms_update_requirement_sections_path '
        'ON gms_update_requirement_sections(level, number, title)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_gms_update_requirement_version_tags_lookup '
        'ON gms_update_requirement_version_tags(android_version, change_kind, section_title)'
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gms_update_changes_run ON gms_update_change_events(run_id, source_key)')


def dataclass_public_json(record: Any, *, exclude: tuple[str, ...] = ()) -> str:
    data = asdict(record)
    for key in exclude:
        data.pop(key, None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def row_public_json(row: sqlite3.Row, *, skip_time: bool = True) -> str:
    data = dict(row)
    if skip_time:
        data.pop('first_seen_at', None)
        data.pop('last_seen_at', None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def record_change(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    source_key: str,
    entity_type: str,
    entity_key: str,
    change_type: str,
    before_json: str,
    after_json: str,
    detected_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO gms_update_change_events (
            run_id, source_key, entity_type, entity_key, change_type,
            before_json, after_json, detected_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, source_key, entity_type, entity_key, change_type, before_json, after_json, detected_at),
    )


def replace_records(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    source_key: str,
    table: str,
    key_column: str,
    records: list[Any],
    columns: list[str],
    entity_type: str,
    timestamp: str,
) -> int:
    existing = {
        row[key_column]: row
        for row in conn.execute(f'SELECT * FROM {table} WHERE source_key = ?', (source_key,)).fetchall()
    }
    incoming = {getattr(record, key_column): record for record in records}
    changes = 0

    for key, row in existing.items():
        if key in incoming:
            continue
        changes += 1
        record_change(
            conn,
            run_id=run_id,
            source_key=source_key,
            entity_type=entity_type,
            entity_key=key,
            change_type='removed',
            before_json=row_public_json(row),
            after_json='',
            detected_at=timestamp,
        )
        conn.execute(f'DELETE FROM {table} WHERE source_key = ? AND {key_column} = ?', (source_key, key))

    insert_columns = [*columns, 'first_seen_at', 'last_seen_at']
    placeholders = ', '.join(['?'] * len(insert_columns))
    update_assignments = ', '.join([f'{column} = excluded.{column}' for column in columns if column not in ('source_key', key_column)])
    update_assignments = f'{update_assignments}, last_seen_at = excluded.last_seen_at'

    for key, record in incoming.items():
        before = existing.get(key)
        after_json = dataclass_public_json(record)
        if before is None:
            changes += 1
            record_change(
                conn,
                run_id=run_id,
                source_key=source_key,
                entity_type=entity_type,
                entity_key=key,
                change_type='added',
                before_json='',
                after_json=after_json,
                detected_at=timestamp,
            )
            first_seen_at = timestamp
        else:
            first_seen_at = before['first_seen_at']
            if before['content_hash'] != getattr(record, 'content_hash'):
                changes += 1
                record_change(
                    conn,
                    run_id=run_id,
                    source_key=source_key,
                    entity_type=entity_type,
                    entity_key=key,
                    change_type='changed',
                    before_json=row_public_json(before),
                    after_json=after_json,
                    detected_at=timestamp,
                )
        values = [getattr(record, column) for column in columns]
        values.extend([first_seen_at, timestamp])
        conn.execute(
            f"""
            INSERT INTO {table} ({', '.join(insert_columns)})
            VALUES ({placeholders})
            ON CONFLICT(source_key, {key_column}) DO UPDATE SET {update_assignments}
            """,
            values,
        )
    return changes


def upsert_source(conn: sqlite3.Connection, fetched: FetchedDocument, timestamp: str) -> bool:
    existing = conn.execute(
        'SELECT content_hash, first_seen_at, last_changed_at FROM gms_update_sources WHERE source_key = ?',
        (fetched.source.key,),
    ).fetchone()
    changed = not existing or existing['content_hash'] != fetched.content_hash
    first_seen_at = existing['first_seen_at'] if existing else timestamp
    last_changed_at = timestamp if changed else existing['last_changed_at']
    conn.execute(
        """
        INSERT INTO gms_update_sources (
            source_key, name, url, final_url, category, parser, auth_required,
            content_hash, status_code, title, first_seen_at, last_scanned_at, last_changed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            name = excluded.name,
            url = excluded.url,
            final_url = excluded.final_url,
            category = excluded.category,
            parser = excluded.parser,
            auth_required = excluded.auth_required,
            content_hash = excluded.content_hash,
            status_code = excluded.status_code,
            title = excluded.title,
            last_scanned_at = excluded.last_scanned_at,
            last_changed_at = excluded.last_changed_at
        """,
        (
            fetched.source.key,
            fetched.source.name,
            fetched.source.url,
            fetched.final_url,
            fetched.source.category,
            fetched.source.parser,
            int(fetched.source.auth_required),
            fetched.content_hash,
            fetched.status_code,
            fetched.title,
            first_seen_at,
            timestamp,
            last_changed_at,
        ),
    )
    return changed


def sync_source(conn: sqlite3.Connection, run_id: int, fetched: FetchedDocument, *, force: bool, timestamp: str) -> tuple[bool, int, ParsedSource]:
    changed = upsert_source(conn, fetched, timestamp)
    if not changed and not force:
        return False, 0, ParsedSource()

    parser = PARSERS[fetched.source.parser]
    parsed = parser(fetched)
    changes = 0
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_artifacts',
        key_column='item_key',
        records=parsed.artifacts,
        columns=[
            'source_key',
            'item_key',
            'suite_type',
            'android_version',
            'release_name',
            'artifact_kind',
            'arch',
            'file_name',
            'download_url',
            'release_notes_url',
            'user_guide_url',
            'ci_build_id',
            'target_platform',
            'description',
            'section_path',
            'content_hash',
        ],
        entity_type='artifact',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_packages',
        key_column='item_key',
        records=parsed.gms_packages,
        columns=[
            'source_key',
            'item_key',
            'section',
            'android_version',
            'release_notes_url',
            'file_name',
            'download_url',
            'required_from',
            'partner_gerrit_tag',
            'partner_gerrit_url',
            'description',
            'content_hash',
        ],
        entity_type='gms_package',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_requirement_sections',
        key_column='section_key',
        records=parsed.requirement_sections,
        columns=[
            'source_key',
            'section_key',
            'level',
            'number',
            'title',
            'path',
            'text_excerpt',
            'table_count',
            'link_count',
            'content_hash',
        ],
        entity_type='requirement_section',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_requirement_table_rows',
        key_column='row_key',
        records=parsed.requirement_table_rows,
        columns=[
            'source_key',
            'row_key',
            'section_key',
            'section_title',
            'table_index',
            'row_index',
            'headers_json',
            'values_json',
            'row_text',
            'content_hash',
        ],
        entity_type='requirement_table_row',
        timestamp=timestamp,
    )
    changes += replace_records(
        conn,
        run_id=run_id,
        source_key=fetched.source.key,
        table='gms_update_requirement_version_tags',
        key_column='tag_key',
        records=parsed.requirement_version_tags,
        columns=[
            'source_key',
            'tag_key',
            'android_version',
            'change_kind',
            'section_key',
            'section_title',
            'requirement_ids',
            'text_excerpt',
            'content_hash',
        ],
        entity_type='requirement_version_tag',
        timestamp=timestamp,
    )
    return True, changes, parsed


def select_sources(source_keys: list[str] | None) -> list[SourceConfig]:
    if not source_keys:
        return list(SOURCES)
    known = {source.key: source for source in SOURCES}
    unknown = sorted(set(source_keys) - set(known))
    if unknown:
        raise ValueError(f'unknown source key(s): {", ".join(unknown)}')
    return [known[key] for key in source_keys]


def create_scan_run(conn: sqlite3.Connection, mode: str, source_filter: str, started_at: str) -> int:
    conn.execute(
        """
        INSERT INTO gms_update_scan_runs (
            mode, source_filter, started_at, finished_at, sources_scanned, sources_skipped,
            artifacts_total, packages_total, requirement_sections_total, requirement_table_rows_total,
            changes_total, success, error
        )
        VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, '')
        """,
        (mode, source_filter, started_at, started_at),
    )
    return int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])


def finish_scan_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    sources_scanned: int,
    sources_skipped: int,
    artifacts_total: int,
    packages_total: int,
    requirement_sections_total: int,
    requirement_table_rows_total: int,
    changes_total: int,
    success: bool,
    error: str = '',
) -> None:
    conn.execute(
        """
        UPDATE gms_update_scan_runs
        SET finished_at = ?, sources_scanned = ?, sources_skipped = ?,
            artifacts_total = ?, packages_total = ?, requirement_sections_total = ?,
            requirement_table_rows_total = ?, changes_total = ?, success = ?, error = ?
        WHERE id = ?
        """,
        (
            finished_at,
            sources_scanned,
            sources_skipped,
            artifacts_total,
            packages_total,
            requirement_sections_total,
            requirement_table_rows_total,
            changes_total,
            int(success),
            error,
            run_id,
        ),
    )


def run_sync(args: argparse.Namespace) -> int:
    selected_sources = select_sources(args.source)
    force = args.mode == 'full'
    started_at = utc_now()
    session = build_session(args)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        source_filter = ','.join(source.key for source in selected_sources)
        run_id = create_scan_run(conn, args.mode, source_filter, started_at)
        conn.commit()

        sources_scanned = 0
        sources_skipped = 0
        artifacts_total = 0
        packages_total = 0
        requirement_sections_total = 0
        requirement_table_rows_total = 0
        changes_total = 0
        try:
            for source in selected_sources:
                if args.verbose:
                    print(f'fetching {source.key}: {source.url}', file=sys.stderr)
                fetched = fetch_source(session, source, args.timeout)
                scanned, changes, parsed = sync_source(conn, run_id, fetched, force=force, timestamp=utc_now())
                if scanned:
                    sources_scanned += 1
                else:
                    sources_skipped += 1
                artifacts_total += len(parsed.artifacts)
                packages_total += len(parsed.gms_packages)
                requirement_sections_total += len(parsed.requirement_sections)
                requirement_table_rows_total += len(parsed.requirement_table_rows)
                changes_total += changes
                if args.verbose:
                    state = 'parsed' if scanned else 'unchanged'
                    print(
                        f'{source.key}: {state}, changes={changes}, artifacts={len(parsed.artifacts)}, '
                        f'packages={len(parsed.gms_packages)}, req_sections={len(parsed.requirement_sections)}, '
                        f'req_rows={len(parsed.requirement_table_rows)}',
                        file=sys.stderr,
                    )
            finish_scan_run(
                conn,
                run_id,
                finished_at=utc_now(),
                sources_scanned=sources_scanned,
                sources_skipped=sources_skipped,
                artifacts_total=artifacts_total,
                packages_total=packages_total,
                requirement_sections_total=requirement_sections_total,
                requirement_table_rows_total=requirement_table_rows_total,
                changes_total=changes_total,
                success=True,
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            init_db(conn)
            finish_scan_run(
                conn,
                run_id,
                finished_at=utc_now(),
                sources_scanned=sources_scanned,
                sources_skipped=sources_skipped,
                artifacts_total=artifacts_total,
                packages_total=packages_total,
                requirement_sections_total=requirement_sections_total,
                requirement_table_rows_total=requirement_table_rows_total,
                changes_total=changes_total,
                success=False,
                error=str(exc),
            )
            conn.commit()
            print(f'error: {exc}', file=sys.stderr)
            return 1

    print(
        f'run_id={run_id} mode={args.mode} sources_scanned={sources_scanned} sources_skipped={sources_skipped} '
        f'artifacts={artifacts_total} packages={packages_total} requirement_sections={requirement_sections_total} '
        f'requirement_table_rows={requirement_table_rows_total} changes={changes_total} db={args.db}'
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Scan CTS/GMS update pages and store structured change records.')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB_PATH, help=f'Default: {DEFAULT_DB_PATH}')
    parser.add_argument('--mode', choices=('incremental', 'full'), default='incremental')
    parser.add_argument('--source', action='append', choices=[source.key for source in SOURCES], help='Source key to scan. Repeatable.')
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--browser', choices=('auto', 'firefox', 'chromium'), default='auto')
    parser.add_argument('--cookie-file', type=Path)
    parser.add_argument('--cookie-header')
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def main() -> int:
    return run_sync(build_parser().parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
