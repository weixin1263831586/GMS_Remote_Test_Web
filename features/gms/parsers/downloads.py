"""CTS, VTS, GTS and GMS package downloads."""

from __future__ import annotations

from ..models import (
    ArtifactRecord,
    FetchedDocument,
    GmsPackageRecord,
    LinkInfo,
    ParsedSource,
)
from ..parsing import (
    article_node,
    cell_first_link,
    cell_text,
    ci_build_id,
    file_name_from_url_or_text,
    infer_arch,
    infer_suite_type,
    is_android_14_or_newer,
    is_arm_arch,
    is_gts_13_or_newer,
    nearest_previous_heading,
    node_links,
    normalize_key,
    normalized_vts_file_name,
    section_siblings,
    stable_hash,
    table_rows,
    text_content,
)


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
                'partner_gerrit_url': tag_link.url, 'release_notes_url': release_notes_link.url,
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
