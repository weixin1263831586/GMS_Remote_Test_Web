"""Section-aware extraction of external HTML document requirements."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from urllib.parse import quote

from lxml import html


_REQUIREMENT = re.compile(r'\[([A-Z][A-Z0-9]*-(?:\d+|SR)-\d+)\]')
_LEVEL = re.compile(
    r'\b(MUST NOT|SHALL NOT|SHOULD NOT|STRONGLY RECOMMENDED|NOT RECOMMENDED|'
    r'MUST|SHALL|REQUIRED|SHOULD|RECOMMENDED|MAY|OPTIONAL)\b'
)
_HEADING = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_BLOCKS = {'p', 'li', 'dt', 'dd', 'pre', 'tr'}


def _text(node):
    return ' '.join(node.text_content().split())


def _own_text(node):
    """Exclude nested lists/rows from a parent's requirement without dropping its own text."""
    pieces = []

    def walk(element):
        if element.text:
            pieces.append(element.text)
        for child in element:
            if isinstance(child.tag, str) and child.tag not in {'ul', 'ol', 'table', 'li', 'tr'}:
                walk(child)
            if child.tail:
                pieces.append(child.tail)

    walk(node)
    return ' '.join(''.join(pieces).split())


def _article(body, kind, android_version):
    root = html.fromstring(body, parser=html.HTMLParser(encoding='utf-8', no_network=True))
    candidates = root.xpath("//div[contains(concat(' ',normalize-space(@class),' '),' devsite-article-body ')]")
    if not candidates:
        candidates = root.xpath('//article|//main')
    if not candidates:
        raise ValueError('Document body not found')
    if kind == 'cdd':
        title = ' '.join(_text(n) for n in root.xpath('//h1'))
        if not re.search(rf'Android\s+{android_version}\s+Compatibility\s+Definition', title, re.I):
            raise ValueError('CDD title/version mismatch')
    article = candidates[0]
    for node in article.xpath('.//script|.//style|.//nav|.//footer|.//aside|.//form|.//del|.//s|.//strike'):
        if node.getparent() is not None:
            node.drop_tree()
    return article


def _candidate_text(node):
    if node.tag == 'tr':
        cells = [_own_text(cell) for cell in node if cell.tag in {'td', 'th'}]
        return ' | '.join(cells)
    return _own_text(node)


def _is_candidate(node):
    if node.tag not in _BLOCKS:
        return False
    # A paragraph within a list item or table row belongs to that enclosing block.
    if node.tag not in {'li', 'tr'} and any(p.tag in {'li', 'tr'} for p in node.iterancestors()):
        return False
    return True


def parse_document(body: bytes, url: str, kind: str, android_version: int | None):
    article = _article(body, kind, android_version)
    section, anchor, condition = '', '', ''
    rows, prose = [], []
    for node in article.iter():
        if node.tag in _HEADING:
            section, anchor, condition = _text(node), node.get('id', ''), ''
            continue
        if not _is_candidate(node):
            continue
        text = _candidate_text(node)
        if not text or not section:
            continue
        parent_conditions = [
            _own_text(parent) for parent in reversed(list(node.iterancestors())) if parent.tag == 'li'
        ]
        if node.tag == 'tr':
            table = next((p for p in node.iterancestors() if p.tag == 'table'), None)
            if table is not None:
                headers = table.xpath('./thead/tr[1]|./tr[1][th]|./tbody/tr[1][th]')
                if headers and headers[0] is not node:
                    parent_conditions.append(_candidate_text(headers[0]))
        context = ' '.join(dict.fromkeys(x for x in [condition, *parent_conditions] if x))
        link = url + ('#' + quote(anchor, safe='-_.:') if anchor else '')
        match = _REQUIREMENT.match(text)
        if match:
            level = _LEVEL.search(text[match.end():])
            rows.append((section, match.group(1), level.group(1) if level else '', context, text, link))
        else:
            prose.append((section, '', '', context, text, link))
            if node.tag == 'p':
                condition = text
    if kind == 'cdd' and len(rows) < 50:
        raise ValueError('CDD requirement coverage unexpectedly low')
    if not rows and not prose:
        raise ValueError('No indexable content')
    return rows + prose


def coverage_report(body, url, kind, android_version):
    """Compare retained definitions with direct DOM definition candidates, preserving multiplicity."""
    article = _article(body, kind, android_version)
    expected = Counter()
    for node in article.xpath('.//li|.//p[not(ancestor::li) and not(ancestor::tr)]|.//tr|.//dt|.//dd|.//pre'):
        if not _is_candidate(node):
            continue
        match = _REQUIREMENT.match(_candidate_text(node))
        if match:
            expected[match.group(1)] += 1
    actual = Counter(row[1] for row in parse_document(body, url, kind, android_version) if row[1])
    return {
        'expected_definitions': sum(expected.values()),
        'indexed_definitions': sum(actual.values()),
        'missing': dict(expected - actual),
        'unexpected': dict(actual - expected),
    }


def document_title(body: bytes):
    root = html.fromstring(body, parser=html.HTMLParser(encoding='utf-8', no_network=True))
    for query in ('//h1', '//title'):
        for node in root.xpath(query):
            title = _text(node)
            if title:
                return title[:160]
    return ''


def document_revision(body: bytes):
    return hashlib.sha256(body).hexdigest()
