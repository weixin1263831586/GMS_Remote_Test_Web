#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

# ruff: noqa: F403, F405, E402
import json
import re
from pathlib import Path

from lxml import html


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1


from .models import *
from .parsing import *


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
