"""Section-aware GMS requirements, independent clauses and coverage evidence."""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from urllib.parse import quote, unquote, urljoin, urlsplit

from lxml import html

from ..models import (
    FetchedDocument,
    ParsedSource,
    RequirementClauseRecord,
    RequirementSectionRecord,
    RequirementTableRowRecord,
    RequirementVersionTagRecord,
)
from ..parsing import (
    article_node,
    classify_requirement_versions,
    heading_level,
    meaningful_requirement_heading,
    normalize_key,
    requirement_ids,
    stable_hash,
    table_rows,
    text_content,
)


_BLOCKS = frozenset(('p', 'li', 'td', 'th', 'dt', 'dd', 'pre', 'div', 'section', 'blockquote',
                     'article', 'main', 'caption', 'figcaption', 'ul', 'ol', 'table', 'tr',
                     'thead', 'tbody', 'tfoot', 'figure'))
_SKIP = frozenset(('script', 'style', 'noscript', 'nav', 'footer', 'button', 'del', 's', 'strike'))
_START = re.compile(r'^Start of (?:(?:requirements (added|changed|removed) in Android\s+)(\d{2}(?:\s+QPR\d+)?)|Android\s+(\d{2}(?:\s+QPR\d+)?)\s+requirements)', re.I)
_END = re.compile(r'^End of (?:added|changed|removed|Android|requirements)', re.I)
_DEFINITION = re.compile(r'^\s*\[([A-Z]+(?:-[A-Z]+)?-[0-9][A-Z0-9_.-]*)\]')
_SCOPE = re.compile(r'^(?:chipset|device|product|software|build)(?:[-_]\d+)?$', re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _scope(node):
    values = []
    for parent in reversed([node, *node.iterancestors()]):
        identity = parent.get('id', '')
        if parent.get('role') == 'tabpanel' or _SCOPE.fullmatch(identity):
            values.append(identity or parent.get('aria-labelledby', '') or 'tabpanel')
        for name in ('data-tab', 'data-tab-id', 'data-tab-label'):
            if parent.get(name):
                values.append(parent.get(name))
    return list(dict.fromkeys(values))


def _safe_url(base, value):
    try:
        resolved = urljoin(base, value or '')
        return resolved if value and urlsplit(resolved).scheme in ('http', 'https') else ''
    except ValueError:
        return ''


def _media(node, base):
    result = []
    for image in node.xpath('self::img|.//img'):
        url = _safe_url(base, image.get('src') or image.get('data-src'))
        result.append({'url': url, 'alt': image.get('alt', ''),
                       'scope': _scope(image), 'available': bool(url)})
    return result


def _policy_heading_level(node):
    level = heading_level(node)
    if level:
        return level
    if node.get('role') == 'heading':
        value = node.get('aria-level', '')
        if value in ('2', '3', '4', '5', '6'):
            return int(value)
    return 0


def _active_policy_article(article):
    """Remove explicitly deleted text from a copy while preserving surrounding text."""
    active = deepcopy(article)
    removed = 0
    for node in list(active.iterdescendants()):
        if not isinstance(node.tag, str):
            continue
        style = node.get('style', '')
        if node.tag not in ('del', 's', 'strike') and not re.search(
            r'(?:^|;)\s*text-decoration(?:-line)?\s*:[^;]*\bline-through\b', style, re.I
        ):
            continue
        if node.getparent() is not None:
            node.drop_tree()
            removed += 1
    return active, removed


def _local_links(node):
    return list(dict.fromkeys(unquote(href[1:]) for href in node.xpath('.//a/@href')
                              if href.startswith('#') and len(href) > 1))


def _note_kind(node, value):
    identity = node.get('id', '')
    if (node.get('role') == 'doc-footnote'
            or re.match(r'^(?:fn|footnote)[-_]?\d', identity, re.I)
            or re.match(r'^Footnotes?\s*[:.]', value, re.I)):
        return 'footnote'
    if re.match(r'^Notes?\s*:', value, re.I):
        return 'note'
    return ''


def _events(node):
    """Walk document order across containers without duplicating nested text."""
    if not isinstance(node.tag, str) or node.tag in _SKIP:
        return
    if _policy_heading_level(node):
        yield 'heading', node, text_content(node)
        return
    if node.tag == 'table':
        yield 'table', node, ''
        return
    if node.tag == 'img':
        yield 'image', node, ''
        return
    pieces = [node.text or '']

    def flush():
        return ' '.join(''.join(pieces).split())

    for child in node:
        if not isinstance(child.tag, str):
            pieces.append(child.tail or '')
            continue
        structural = child.tag in _BLOCKS or _policy_heading_level(child) or child.tag == 'img'
        if structural:
            value = flush()
            if value:
                yield 'text', node, value
            pieces.clear()
            yield from _events(child)
        elif child.tag not in _SKIP:
            # Inline wrappers can themselves contain block nodes.
            if any(isinstance(desc.tag, str) and (desc.tag in _BLOCKS or _policy_heading_level(desc)
                   or desc.tag == 'img') for desc in child.iterdescendants()):
                value = flush()
                if value:
                    yield 'text', node, value
                pieces.clear()
                yield from _events(child)
            else:
                pieces.append(' ' if child.tag == 'br' else child.text_content())
        pieces.append(child.tail or '')
    value = flush()
    if value:
        yield 'text', node, value


def section_key_from_heading(heading: html.HtmlElement, path: str) -> tuple[str, str, str]:
    title = text_content(heading)
    match = re.match(r'^(\d+(?:\.\d+)*)\.?\s*(.*)$', title)
    number = match.group(1) if match else ''
    clean = match.group(2).strip() if match else title
    return normalize_key(path or title), number, clean


def parse_gms_requirements(fetched: FetchedDocument, session=None) -> ParsedSource:
    raw_article = article_node(fetched.doc)
    revision = stable_hash(html.tostring(raw_article, encoding='unicode', with_tail=False))
    article, removed_revision_nodes = _active_policy_article(raw_article)
    source = fetched.source.key
    base = fetched.final_url.split('#', 1)[0]
    anchor_counts = Counter(article.xpath('.//@id') + article.xpath('.//a/@name'))
    anchors = {key for key, count in anchor_counts.items() if count == 1}
    table_ids = set()
    table_definition_candidates = set()
    headings = {}
    sections = {}
    section_occurrences = Counter()
    clause_occurrences = Counter()
    row_occurrences = Counter()
    clauses = []
    rows = []
    tags = {}
    media = []
    references = Counter()
    marker_counts = Counter()
    markers = []
    orphan_ends = 0
    tables = 0
    current = None
    context = {}
    clause_owner = {}
    table_owner = {}
    evidence_links = {}

    def select_section(node, level, title):
        nonlocal current
        for key in list(headings):
            if key >= level:
                del headings[key]
        headings[level] = title
        path = ' / '.join(headings.values())
        key, number, clean = section_key_from_heading(node, path)
        occurrence = section_occurrences[key]
        section_occurrences[key] += 1
        if occurrence:
            key = normalize_key(path, node.get('id', ''), _json(_scope(node)), str(occurrence))
        current = key
        sections[key] = {'level': level, 'number': number, 'title': clean, 'path': path,
                         'anchor': node.get('id', ''), 'parts': [title], 'tables': 0, 'links': []}
        context.clear()
        clause_owner.clear()
        table_owner.clear()

    def add_tag(version, kind, ids, value, path, section_key):
        ids_text = ', '.join(ids)
        key = normalize_key(source, version, kind, section_key, ids_text, value)
        tags[key] = RequirementVersionTagRecord(
            source, key, version, kind, section_key, path, ids_text, value,
            stable_hash([source, version, kind, section_key, ids_text, value]),
        )

    for event, node, value in _events(article):
        if event == 'heading':
            level = _policy_heading_level(node)
            if level >= 2 and meaningful_requirement_heading(value, level):
                select_section(node, level, value)
                continue
            if level == 1:
                continue
            event = 'text'
        if current is None:
            # Preserve front matter without inventing a numbered chapter.
            current = normalize_key('document front matter')
            sections[current] = {'level': 1, 'number': '', 'title': 'Document front matter',
                                 'path': 'Document front matter', 'anchor': '',
                                 'parts': [], 'tables': 0, 'links': []}
        section = sections[current]
        links = evidence_links.setdefault(current, {'tables': [], 'notes': []})
        path = section['path']
        scope = _scope(node)
        scope_key = tuple(scope)
        if event == 'image':
            media.extend({**item, 'section_key': current} for item in _media(node, base))
            continue
        if event == 'table':
            tables += 1
            section['tables'] += 1
            # Table rows are indexed independently; IDs in a table are not necessarily
            # complete clause definitions or evidence of a version's applicability.
            table_ids.update(requirement_ids(text_content(node)))
            for cell in node.xpath('.//td|.//th'):
                definition = _DEFINITION.match(text_content(cell))
                if definition:
                    table_definition_candidates.add(definition.group(1))
            media.extend({**item, 'section_key': current} for item in _media(node, base))
            headers, parsed_rows = table_rows(node, base, complete_headers=True)
            links['tables'].append({'table_index': tables, 'clause_key': clause_owner.get(scope_key, ''),
                                    'scope': scope, 'local_links': _local_links(node),
                                    'association': 'adjacent_candidate'})
            table_owner[scope_key] = tables
            for row in parsed_rows:
                values = row['values']
                headers_json, values_json = _json(headers), _json(values)
                identity = (current, headers_json, values_json)
                # Count occurrences by the same normalized identity used for row keys.
                normalized = normalize_key(source, *identity)
                occurrence = row_occurrences[normalized]
                row_occurrences[normalized] += 1
                rows.append(RequirementTableRowRecord(
                    source, normalize_key(source, *identity, str(occurrence)), current, path,
                    tables, row['row_index'], headers_json, values_json, ' | '.join(values),
                    stable_hash([headers, values]),
                ))
            continue
        offset = section.get('text_length', len(' '.join(section['parts']))) + bool(section['parts'])
        section['parts'].append(value)
        section['text_length'] = offset + len(value)
        note_kind = _note_kind(node, value)
        if note_kind:
            anchor = node.get('id', '')
            links['notes'].append({'kind': note_kind, 'start': offset, 'end': offset + len(value),
                                   'anchor': anchor if anchor in anchors else '', 'scope': scope,
                                   'clause_key': clause_owner.get(scope_key, ''),
                                   'table_index': table_owner.get(scope_key),
                                   'association': 'adjacent_candidate'})
        section['links'].extend(filter(None, (_safe_url(base, href) for href in node.xpath('.//a/@href'))))
        ids = requirement_ids(value)
        references.update(ids)
        start = _START.match(value)
        if start:
            kind = (start.group(1) or 'added').lower()
            version = 'Android ' + ' '.join((start.group(2) or start.group(3)).upper().split())
            markers.append((version, kind, scope_key))
            clause_owner.pop(scope_key, None)
            table_owner.pop(scope_key, None)
            marker_counts[(version, kind)] += 1
            add_tag(version, kind, ids, value, path, current)
            continue
        if _END.match(value):
            clause_owner.pop(scope_key, None)
            table_owner.pop(scope_key, None)
            if markers and markers[-1][2] == scope_key:
                markers.pop()
            else:
                orphan_ends += 1
            continue
        active = next(((v, k) for v, k, s in reversed(markers) if s == scope_key), ('', ''))
        mentions = list(dict.fromkeys(v for v, _ in classify_requirement_versions(value)))
        definition = _DEFINITION.match(value)
        if definition and node.tag != 'table' and next(node.iterancestors('table'), None) is None:
            requirement_id = definition.group(1)
            # A bracketed range or a non-applicable statement remains a candidate,
            # never an assertion that every ID in the range is a definition.
            condition = context.get(scope_key, '')
            local_anchors = set(node.xpath('self::*/@id|.//@id|self::a/@name|.//a/@name'))
            clause_anchor = requirement_id in anchors and requirement_id in local_anchors
            anchor = requirement_id if clause_anchor else section['anchor']
            anchor_kind = 'clause' if clause_anchor else 'section' if anchor in anchors else 'document'
            url = base + ('#' + quote(anchor, safe='-_.:') if anchor_kind != 'document' else '')
            payload = {'id': requirement_id, 'section': current, 'scope': scope, 'text': value,
                       'context': condition, 'marker': active, 'mentions': mentions,
                       'url': url, 'references': [i for i in ids if i != requirement_id],
                       'media': _media(node, base)}
            identity = (current, requirement_id, _json(scope))
            normalized = normalize_key(source, *identity)
            ordinal = clause_occurrences[normalized]
            clause_occurrences[normalized] += 1
            clauses.append(RequirementClauseRecord(
                source, normalize_key(source, *identity, str(ordinal)), requirement_id, current, path,
                value, condition, active[0], active[1], _json(mentions), _json(scope), url, anchor_kind,
                revision, _json(payload['references']), _json(payload['media']), stable_hash(payload),
            ))
            clause_owner[scope_key] = clauses[-1].clause_key
            table_owner.pop(scope_key, None)
            if active[0]:
                add_tag(active[0], active[1], [requirement_id], value, path, current)
        elif not ids and not note_kind and node.tag in ('p', 'li', 'td', 'th', 'div', 'dt', 'dd'):
            context[scope_key] = value
            clause_owner.pop(scope_key, None)
            table_owner.pop(scope_key, None)
        for version in mentions:
            if version != active[0]:
                add_tag(version, 'specific', ids, value, path, current)

    section_records = [
        RequirementSectionRecord(source, key, item['level'], item['number'], item['title'], item['path'],
                                 ' '.join(item['parts']), item['tables'], len(set(item['links'])),
                                 stable_hash([item['parts'], item['links']]))
        for key, item in sections.items() if any(item['parts']) or item['tables']
    ]
    definitions = Counter(item.requirement_id for item in clauses)
    raw_ids = set(requirement_ids(text_content(article)))
    covered = set(references) | table_ids
    media_total = len(article.xpath('.//img'))
    coverage = {
        'source_revision': revision,
        'revision_text_scope': 'active_markup_only',
        'historical_applicability_verified': False,
        'section_evidence_links': {key: value for key, value in evidence_links.items()
                                   if value['tables'] or value['notes']},
        'removed_revision_nodes': removed_revision_nodes,
        'section_anchors': {key: value['anchor'] for key, value in sections.items()
                            if value['anchor'] in anchors},
        'source_scope': 'fetched_html_only',
        'coverage_complete': False,
        'sections': len(section_records), 'tables': tables, 'table_rows': len(rows),
        'clause_candidates': len(clauses), 'unique_definition_ids': len(definitions),
        'raw_unique_ids': len(raw_ids), 'observed_unique_ids': len(covered),
        'missing_observed_ids': sorted(raw_ids - covered),
        'reference_only_ids': sorted(covered - set(definitions)),
        'duplicate_definition_ids': {key: n for key, n in definitions.items() if n > 1},
        'duplicate_anchor_count': sum(count > 1 for count in anchor_counts.values()),
        'table_observed_ids': sorted(table_ids),
        'table_only_definition_candidates': sorted(table_definition_candidates - set(definitions)),
        'media_inventory_count': media_total,
        'media_not_in_event_inventory': max(0, media_total - len(media)),
        'applicability_verified': False,
        'original_document_verified': False,
        'marker_counts': {f'{version}/{kind}': count for (version, kind), count in marker_counts.items()},
        'unclosed_markers': len(markers), 'orphan_end_markers': orphan_ends,
        'media': media, 'unresolved_media': sum(not item['available'] for item in media),
        'limitation': ('Fetched DOM candidates only; table definitions, remote tabs, media content, '
                       'applicability and PDF equivalence require independent verification.'),
    }
    return ParsedSource(requirement_sections=section_records, requirement_table_rows=rows,
                        requirement_version_tags=list(tags.values()), requirement_clauses=clauses,
                        requirement_coverage=coverage)
