"""GMS requirements sections, tables and version markers."""

from __future__ import annotations

import json
import re

from lxml import html

from ..models import (
    FetchedDocument,
    ParsedSource,
    RequirementSectionRecord,
    RequirementTableRowRecord,
    RequirementVersionTagRecord,
)
from ..parsing import (
    article_node,
    classify_marker_kind,
    classify_requirement_versions,
    focused_section_siblings,
    heading_level,
    heading_nodes,
    meaningful_requirement_heading,
    nearest_requirement_path,
    node_links,
    normalize_key,
    requirement_candidate_nodes,
    requirement_ids,
    stable_hash,
    table_rows,
    text_content,
)


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
    headings = heading_nodes(article, levels=(2, 3, 4, 5, 6))
    heading_path: dict[int, str] = {}
    table_index = 0
    row_occurrences = {}
    for heading in headings:
        level = heading_level(heading)
        title = text_content(heading)
        if not meaningful_requirement_heading(title, level):
            continue
        heading_path = {key: value for key, value in heading_path.items() if key < level}
        heading_path[level] = title
        path = ' / '.join(heading_path.values())
        section_key, number, clean = section_key_from_heading(heading, path)
        siblings = focused_section_siblings(heading)
        section_text = ' '.join([title, *[text_content(sibling) for sibling in siblings]])
        section_hash = stable_hash({'text': section_text, 'links': [link.url for sibling in siblings for link in node_links(sibling, fetched.final_url)]})
        tables = [table for sibling in siblings for table in
                  ([sibling] if sibling.tag == 'table' else sibling.xpath('.//table'))]
        links = [link for sibling in siblings for link in node_links(sibling, fetched.final_url)]
        sections.append(
            RequirementSectionRecord(
                source_key=fetched.source.key,
                section_key=section_key,
                level=level,
                number=number,
                title=clean,
                path=path,
                text_excerpt=section_text,
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
            if not ids and not re.search(r'Android\s+\d{2}|Start of requirements', candidate_text, re.I):
                continue
            classifications = classify_requirement_versions(candidate_text, marker_kind)
            if not classifications:
                continue
            ids_text = ', '.join(ids)
            candidate_path = nearest_requirement_path(candidate, path)
            candidate_section_key = normalize_key(candidate_path)
            for android_version, change_kind in classifications:
                tag_key = normalize_key(fetched.source.key, android_version, change_kind, candidate_section_key, ids_text, candidate_text)
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
                        text_excerpt=candidate_text,
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
                # Position is display metadata, not identity: inserting a table must not
                # churn every subsequent unchanged record. Preserve repeated identical rows.
                identity = (section_key, headers_json, values_json)
                occurrence = row_occurrences.get(identity, 0)
                row_occurrences[identity] = occurrence + 1
                table_rows_out.append(
                    RequirementTableRowRecord(
                        source_key=fetched.source.key,
                        row_key=normalize_key(fetched.source.key, section_key, headers_json, values_json, str(occurrence)),
                        section_key=section_key,
                        section_title=path,
                        table_index=table_index,
                        row_index=row_index,
                        headers_json=headers_json,
                        values_json=values_json,
                        row_text=row_text,
                        content_hash=row_hash,
                    )
                )
    return ParsedSource(
        requirement_sections=sections,
        requirement_table_rows=table_rows_out,
        requirement_version_tags=version_tags,
    )
