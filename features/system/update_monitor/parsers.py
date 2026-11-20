#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from urllib.parse import urljoin

from lxml import html

from .fetching import MAINLINE_MONTH_DEPTH, build_train_url, fetch_html, recent_month_cutoff
from .models import (
    ArtifactRecord,
    BulletinPatchRecord,
    FetchedDocument,
    GmsPackageRecord,
    LinkInfo,
    MainlinePackageRecord,
    ParsedSource,
    RequirementSectionRecord,
    RequirementTableRowRecord,
    RequirementVersionTagRecord,
)
from .parsing import (
    article_node,
    cell_first_link,
    cell_text,
    ci_build_id,
    classify_marker_kind,
    classify_requirement_versions,
    file_name_from_url_or_text,
    focused_section_siblings,
    heading_level,
    heading_nodes,
    infer_arch,
    infer_suite_type,
    is_android_14_or_newer,
    is_arm_arch,
    is_gts_13_or_newer,
    meaningful_requirement_heading,
    nearest_previous_heading,
    nearest_requirement_path,
    node_links,
    normalize_key,
    normalized_vts_file_name,
    requirement_candidate_nodes,
    requirement_ids,
    section_siblings,
    stable_hash,
    table_rows,
    text_content,
    utc_now,
)


# 月份目录允许缩写和全称，统一规范为三字母键。
_MAINLINE_MONTHS = {
    'jan': (1, 'January'),
    'january': (1, 'January'),
    'feb': (2, 'February'),
    'february': (2, 'February'),
    'mar': (3, 'March'),
    'march': (3, 'March'),
    'apr': (4, 'April'),
    'april': (4, 'April'),
    'may': (5, 'May'),
    'jun': (6, 'June'),
    'june': (6, 'June'),
    'jul': (7, 'July'),
    'july': (7, 'July'),
    'aug': (8, 'August'),
    'august': (8, 'August'),
    'sep': (9, 'September'),
    'september': (9, 'September'),
    'oct': (10, 'October'),
    'october': (10, 'October'),
    'nov': (11, 'November'),
    'november': (11, 'November'),
    'dec': (12, 'December'),
    'december': (12, 'December'),
}

# 月份数字到三字母键的映射。
_MONTH_INDEX_TO_KEY = {idx: key for key, (idx, _label) in _MAINLINE_MONTHS.items() if len(key) == 3}

# 匹配 PRELOAD 页面中的构建 ID。
_MAINLINE_BUILD_ID_RE = re.compile(r'\b(\d{6,})\b')
# 匹配 PRELOAD 发布说明链接。
_MAINLINE_PRELOAD_LINK_RE = re.compile(r'/release-notes/(\d{4})/([a-z]+)/(notes-PRELOAD-[^/?#]+)', re.IGNORECASE)
# 从 PRELOAD slug 提取实际构建日期。
_MAINLINE_SLUG_DATE_RE = re.compile(r'notes-PRELOAD-(\d{4})-(\d{2})-(\d{2})', re.IGNORECASE)


def _extract_mainline_links(doc: html.HtmlElement, base_url: str) -> list[tuple[int, int, str, str]]:
    """Return ``[(year, month_index, preload_slug, notes_url), ...]`` from index page links.

    ``year`` and ``month_index`` come from the **URL directory month**
    (``/release-notes/2026/may/...``), not the slug date. The slug date is the
    document publication date and often falls in the next calendar month (e.g.
    the 'may' directory holds builds published on 06-09 and 06-11). The
    directory month is how the release is grouped on the index page and is what
    users expect in the UI.
    """
    seen: set[str] = set()
    found: list[tuple[int, int, str, str]] = []
    for href in doc.xpath('//a/@href'):
        match = _MAINLINE_PRELOAD_LINK_RE.search(href or '')
        if not match:
            continue
        dir_year, dir_month, slug = match.group(1), match.group(2).lower(), match.group(3)
        if dir_month not in _MAINLINE_MONTHS:
            continue
        if not _MAINLINE_SLUG_DATE_RE.search(slug):
            continue
        year = int(dir_year)
        month_index = _MAINLINE_MONTHS[dir_month][0]
        key = f'{year}|{month_index}|{slug}'
        if key in seen:
            continue
        seen.add(key)
        notes_url = urljoin(base_url, href.split('?')[0] + '?authuser=2')
        found.append((year, month_index, slug, notes_url))
    return found


def _filter_recent_mainline(
    entries: list[tuple[int, int, str, str]], depth: int, *, now_year: int | None = None, now_month: int | None = None
) -> list[tuple[int, int, str, str]]:
    """Keep only the most recent ``depth`` months, newest first."""
    cutoff_year, cutoff_month = recent_month_cutoff(depth, now_year=now_year, now_month=now_month)
    cutoff_total = cutoff_year * 12 + (cutoff_month - 1)

    def keep(entry: tuple[int, int, str, str]) -> bool:
        return entry[0] * 12 + (entry[1] - 1) >= cutoff_total

    filtered = [entry for entry in entries if keep(entry)]
    # Newest first by (year, month), stable on preload slug. The month window
    # above is the only limit: one month can hold several builds, so slicing
    # to ``depth`` entries would drop the oldest in-window records.
    filtered.sort(key=lambda e: (e[0], e[1], e[2]), reverse=True)
    return filtered


def _partner_zip_build_id(doc: html.HtmlElement) -> tuple[str, str]:
    """Locate the "Preload partner zip" row and return ``(build_id, label)``.

    Defensive: the exact DOM is only visible behind auth at runtime, so we try
    several heuristics — a table row whose first cell mentions "preload partner
    zip", then any table row, then a page-wide scan for a build id near that
    phrase. Returns ``('', '')`` when nothing is found.
    """
    # 1. Table row whose label cell mentions "preload partner zip".
    label_cells = [
        cell
        for cell in doc.xpath('//td | //th')
        if 'preload partner zip' in (text_content(cell) or '').lower()
    ]
    for cell in label_cells:
        row = cell.getparent()
        if row is None:
            continue
        row_text = text_content(row)
        match = _MAINLINE_BUILD_ID_RE.search(row_text)
        if match:
            return match.group(1), row_text.strip()

    # 2. 查找同时包含目标文本和构建 ID 的表格行。
    phrase = 'preload partner zip'
    for row in doc.xpath('//tr'):
        row_text = text_content(row)
        if phrase in row_text.lower():
            match = _MAINLINE_BUILD_ID_RE.search(row_text)
            if match:
                return match.group(1), row_text.strip()

    # 3. 回退到目标文本附近的首个构建 ID。
    page_text = text_content(doc)
    lower = page_text.lower()
    idx = lower.find(phrase)
    if idx != -1:
        window = page_text[idx:idx + 200]
        match = _MAINLINE_BUILD_ID_RE.search(window)
        if match:
            return match.group(1), phrase
    return '', ''


def parse_mainline_release_notes(fetched: FetchedDocument, session=None, *, timeout: float = 30.0, depth: int | None = None) -> ParsedSource:
    """Crawl the Mainline release-notes index for recent PRELOAD builds.

    The index page lists year/month → ``notes-PRELOAD-...`` links. We keep the
    most recent ``depth`` months (default ``MAINLINE_MONTH_DEPTH``), fetch each
    PRELOAD page with the authenticated session, and extract the "Preload
    partner zip" build number, mapping it to a CI build URL.
    """
    from datetime import date

    depth = MAINLINE_MONTH_DEPTH if depth is None else depth
    packages: list[MainlinePackageRecord] = []

    entries = _filter_recent_mainline(
        _extract_mainline_links(fetched.doc, fetched.final_url),
        depth,
        now_year=date.today().year,
        now_month=date.today().month,
    )

    for year, month_index, slug, notes_url in entries:
        # 按构建月份数字输出 API 使用的三字母键。
        month_key = _MONTH_INDEX_TO_KEY.get(month_index, '')
        month_label = _MAINLINE_MONTHS.get(month_key, (month_index, ''))[1]

        build_id, label = '', ''
        status, _final, text = fetch_html(session, notes_url, timeout)
        if status == 200 and text:
            try:
                child_doc = html.fromstring(text)
            except Exception:
                child_doc = None
            if child_doc is not None:
                build_id, label = _partner_zip_build_id(child_doc)

        payload = {
            'source_key': fetched.source.key,
            'year': str(year),
            'month': month_key,
            'preload_version': slug,
            'partner_zip_build_id': build_id,
        }
        packages.append(
            MainlinePackageRecord(
                source_key=fetched.source.key,
                item_key=normalize_key(fetched.source.key, str(year), month_key, slug),
                year=str(year),
                month=month_key,
                month_label=month_label,
                preload_version=slug,
                notes_url=notes_url,
                partner_zip_build_id=build_id,
                ci_build_url=build_train_url(build_id) if build_id else '',
                partner_zip_label=label,
                content_hash=stable_hash(payload),
            )
        )

    return ParsedSource(mainline_packages=packages)


def parse_cts_downloads(fetched: FetchedDocument, session=None) -> ParsedSource:
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


def parse_vts_downloads(fetched: FetchedDocument, session=None) -> ParsedSource:
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


def parse_gts_downloads(fetched: FetchedDocument, session=None) -> ParsedSource:
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


def parse_gms_downloads(fetched: FetchedDocument, session=None) -> ParsedSource:
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
                    # The row's values belong to the trailing headers, so
                    # rebuild by_header right-aligned before any get() below.
                    by_header = dict(zip(headers[missing_cells:], values))
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


def parse_gms_requirements(fetched: FetchedDocument, session=None) -> ParsedSource:
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


_BULLETIN_VERSION_CELL = re.compile(r'(?:Android\s+)?(\d+)(?:\s+(QPR\d))?')
_BULLETIN_TITLE_SCOPE = re.compile(r'-\s*Android\s+(.+)$')
_BULLETIN_TITLE_PAREN = re.compile(r'\((\d+(?:\s+and\s+\d+)*)\)')
_BULLETIN_TITLE_SPLIT = re.compile(r',|\band\b|\|')
_BULLETIN_BODY_VERSIONS = re.compile(r'Android\s+(\d+(?:\s+QPR\d)?)')


def _normalize_bulletin_version(token: str) -> str | None:
    """把 "16" / "16 QPR2" / "Android 16" 规范成 "16" / "16 QPR2"。"""
    match = _BULLETIN_VERSION_CELL.fullmatch(token.strip())
    if not match:
        return None
    number, qpr = match.group(1), match.group(2)
    return f'{number} {qpr}' if qpr else number


def _sort_bulletin_versions(versions) -> list[str]:
    return sorted(set(versions), key=lambda value: (int(value.split()[0]), value))


def _bulletin_title_versions(title: str) -> list[str]:
    """页面标题的版本范围："... Bulletin - Android 16, 16 QPR2, and 17"。

    后续版本不带 "Android" 前缀，因此正文正则会漏掉 "16 QPR2, and 17"。
    """
    match = _BULLETIN_TITLE_SCOPE.search(title)
    if not match:
        # 旧公告标题格式不同："Nov 2020 Update Bulletin (10 and 11)"。
        paren = _BULLETIN_TITLE_PAREN.search(title)
        if not paren:
            return []
        return _sort_bulletin_versions(
            version for version in (
                _normalize_bulletin_version(token)
                for token in paren.group(1).split('and')
            ) if version
        )
    versions: set[str] = set()
    for token in _BULLETIN_TITLE_SPLIT.split(match.group(1)):
        version = _normalize_bulletin_version(token)
        if version:
            versions.add(version)
        elif token.strip():
            break  # 枚举在首个非版本片段处结束（页尾 "| Android Partners" 等）
    return _sort_bulletin_versions(versions)


def _bulletin_section_versions(nodes: list, title_versions: list[str]) -> list[str]:
    """提取单个补丁小节覆盖的全部 Android 版本。

    小节内 "Android version" 表每行对应一个适用版本（同一补丁跨
    Android 16 / 16 QPR2 / 17 各有不同的提交与下载），是权威来源；
    缺表时回退到正文 "Android N" 提法与页面标题枚举的并集。
    """
    versions: set[str] = set()
    for node in nodes:
        tables = [node] if node.tag == 'table' else node.xpath('.//table')
        for table in tables:
            header_cells = table.xpath('.//tr[1]/th')
            headers = [text_content(cell).strip().lower() for cell in header_cells]
            if not headers or not headers[0].startswith('android version'):
                continue
            for row in table.xpath('.//tr'):
                cells = row.xpath('./td')
                if not cells:
                    continue
                version = _normalize_bulletin_version(text_content(cells[0]))
                if version:
                    versions.add(version)
    if versions:
        return _sort_bulletin_versions(versions)
    for node in nodes:
        versions.update(_BULLETIN_BODY_VERSIONS.findall(text_content(node)))
    return _sort_bulletin_versions(versions | set(title_versions))


def bulletin_content_hash(record: BulletinPatchRecord) -> str:
    """Track content changes without counting fetch timestamps as new fixes."""
    return stable_hash({key: getattr(record, key) for key in (
        'source_key', 'year', 'patch_id', 'description', 'bulletin_month', 'deadline',
        'bulletin_url', 'patch_kind', 'android_versions', 'detail_text',
    )})


def parse_gms_bulletins(fetched: FetchedDocument, session=None, *, timeout: float = 30.0) -> ParsedSource:
    """Parse Critical Fix Supplementary Bulletin tables (per-year sections).

    The overview page's Bulletin cells link to per-month bulletin pages
    (``/partners/releases/cf/<month>-<year>#<patch_id>``); each detail page
    groups patches under "Mandatory" vs "Strongly recommended" headings and
    annotates the affected Android versions. We store the link plus the
    kind/versions and per-patch detail text for full-content recall.
    """
    patches: list[BulletinPatchRecord] = []
    article = fetched.doc.xpath('//article[contains(@class, "devsite-article")]')
    root = article[0] if article else fetched.doc

    headings = root.xpath('.//*[self::h2 or self::h3]')
    for heading in headings:
        year_match = re.fullmatch(r'(\d{4})\s+bulletins?', text_content(heading))
        if not year_match:
            continue
        year = year_match.group(1)
        level = heading_level(heading)
        # 同级及更高级标题之间的兄弟节点里找表格。
        for sibling in heading.itersiblings():
            sibling_level = heading_level(sibling)
            if sibling_level and sibling_level <= level:
                break
            # 表格可能是 sibling 自身（article 直下）或其后人（div 包裹）。
            tables = ([sibling] if sibling.tag == 'table' else []) + sibling.xpath('.//table')
            for table in tables:
                for row in table.xpath('.//tr'):
                    cells = [text_content(cell) for cell in row.xpath('./td|./th')]
                    if len(cells) < 3:
                        continue
                    patch_match = re.match(r'(A-\d+)\s*(.*)', cells[0])
                    if not patch_match:
                        continue
                    patch_id, description = patch_match.group(1), patch_match.group(2)
                    # Bulletin 列的链接 → 月度详情页（#锚点为补丁 ID 数字）。
                    month_link = row.xpath('.//td//a[@href]')
                    bulletin_url = ''
                    for link_el in month_link:
                        href = link_el.get('href') or ''
                        if '/partners/releases/cf/' in href:
                            bulletin_url = urljoin(fetched.final_url, href)
                            break
                    payload = {
                        'source_key': fetched.source.key,
                        'year': year,
                        'patch_id': patch_id,
                        'description': description,
                        'bulletin_month': cells[1],
                        'deadline': cells[2],
                    }
                    patches.append(
                        BulletinPatchRecord(
                            source_key=fetched.source.key,
                            item_key=normalize_key(fetched.source.key, year, patch_id),
                            year=year,
                            patch_id=patch_id,
                            patch_kind='',
                            android_versions='',
                            description=description,
                            bulletin_month=cells[1],
                            deadline=cells[2],
                            bulletin_url=bulletin_url,
                            content_hash=stable_hash(payload),
                        )
                    )

    # 详情页补齐分类（Mandatory / Strongly recommended）与 Android 版本。
    # 每个月度页只抓一次并缓存：53 个补丁通常集中在 ~12 个详情页，不去重
    # 的重复请求会触发 Google 反爬软封（返回 200 空壳页，无法解析）。
    detail_cache: dict[str, tuple[object, list[str]]] = {}
    patch_kinds: dict[str, str] = {}
    patch_versions: dict[str, str] = {}
    patch_details: dict[str, str] = {}
    for patch in patches:
        detail_url = patch.bulletin_url.split('#', 1)[0]
        if not detail_url or not session:
            continue
        if detail_url not in detail_cache:
            time.sleep(0.4)
            status, _final, detail_text = fetch_html(session, detail_url, timeout)
            detail_doc = None
            if status == 200 and detail_text:
                try:
                    detail_doc = html.fromstring(detail_text)
                except Exception:
                    detail_doc = None
            if detail_doc is not None and not detail_doc.xpath('//article[contains(@class, "devsite-article")]'):
                detail_doc = None  # 反爬软封页：200 但无正文，按未富化处理
            page_title_versions: list[str] = []
            if detail_doc is not None:
                og_titles = detail_doc.xpath('//meta[@property="og:title"]/@content')
                if og_titles:
                    page_title_versions = _bulletin_title_versions(og_titles[0])
            detail_cache[detail_url] = (detail_doc, page_title_versions)
        detail_root, page_title_versions = detail_cache[detail_url]
        if detail_root is None:
            continue
        patch_kind = ''
        android_versions: list[str] = []
        for section in detail_root.xpath('.//h2'):
            section_title = text_content(section).lower()
            if 'mandatory' in section_title:
                section_kind = 'mandatory'
            elif 'strongly recommended' in section_title:
                section_kind = 'strongly_recommended'
            else:
                continue
            for heading in section.itersiblings():
                if heading.tag == 'h2':
                    break
                if heading.tag != 'h3':
                    continue
                heading_text = text_content(heading)
                if not re.search(rf'(?<![A-Za-z0-9-]){re.escape(patch.patch_id)}(?![A-Za-z0-9-])', heading_text):
                    continue
                patch_kind = section_kind
                section_nodes = []
                for node in heading.itersiblings():
                    if node.tag in ('h2', 'h3'):
                        break
                    section_nodes.append(node)
                android_versions = _bulletin_section_versions(section_nodes, page_title_versions)
                # Keep only this patch's section: indexing the whole monthly
                # page under every patch would attribute neighbouring CVEs to it.
                texts = [heading_text]
                for node in section_nodes:
                    texts.append(text_content(node))
                    texts.extend(link.url for link in node_links(node, detail_url))
                patch_details[patch.item_key] = '\n'.join(text for text in texts if text)
                break
            if patch_kind:
                break
        if patch_kind or android_versions:
            patch_kinds[patch.item_key] = patch_kind
            patch_versions[patch.item_key] = ', '.join(f'Android {v}' for v in android_versions)

    enriched = []
    for patch in patches:
        detail = patch_details.get(patch.item_key, '')
        patch = replace(
            patch, patch_kind=patch_kinds.get(patch.item_key, ''),
            android_versions=patch_versions.get(patch.item_key, ''), detail_text=detail,
            detail_status='ready' if detail else 'unavailable' if patch.bulletin_url else 'not_applicable',
            detail_fetched_at=utc_now() if detail else '',
        )
        enriched.append(replace(patch, content_hash=bulletin_content_hash(patch)))
    patches = enriched

    return ParsedSource(bulletin_patches=patches)


PARSERS = {
    'cts_downloads': parse_cts_downloads,
    'vts_downloads': parse_vts_downloads,
    'gts_downloads': parse_gts_downloads,
    'gms_downloads': parse_gms_downloads,
    'gms_requirements': parse_gms_requirements,
    'mainline_release_notes': parse_mainline_release_notes,
    'gms_bulletins': parse_gms_bulletins,
}
