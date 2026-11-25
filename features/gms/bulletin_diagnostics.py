"""Local-only, read-only structural diagnostics for failed Critical Fix months.

Run on the authenticated scanning host. Never upload output containing partner
document headings or other restricted source material to public CI artifacts.
"""

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from lxml import html

from .fetching import build_session, fetch_html
from .parsers.critical import (
    _cf_category,
    _cf_category_blocks,
    _cf_content_root,
    _cf_empty_block,
    _cf_explicitly_empty,
    _cf_heading_match,
    _cf_legacy_sections,
    _cf_month_link,
    _cf_month_sections,
    _cf_toc_headings,
    _cf_toc_patch_ids,
)
from .parsing import text_content
from .repository import DB_PATH
from .sources import SOURCES


def describe(doc, url, *, show_labels=False):
    articles = doc.xpath('//article[contains(@class, "devsite-article")]')
    if not articles:
        return {'article': False}
    root = articles[0]
    headings = root.xpath('.//h1|.//h2|.//h3|.//h4|.//h5|.//h6')
    sections = list(_cf_month_sections(root, _cf_toc_headings(doc, url)))
    kinds = Counter(kind or 'unclassified' for kind, *_ in sections)
    toc = _cf_toc_patch_ids(doc, url)
    parsed = {match.group('id') for _, match, _, _ in sections}
    legacy, legacy_complete = _cf_legacy_sections(root) if not sections else ([], False)
    blocks = list(_cf_category_blocks(_cf_content_root(root)))
    report = {
        'category_block_count': len(blocks),
        'empty_category_block_count': sum(_cf_empty_block(nodes) for _, _, nodes in blocks),
        'content_body_found': _cf_content_root(root) is not root,
        'category_block_shapes': [
            {'kind': kind, 'none_count': sum(text_content(node).strip().casefold().rstrip('.') == 'none' for node in nodes),
             'node_count': len(nodes), 'text_chars': sum(len(text_content(node)) for node in nodes),
             'heading_count': sum(1 for node in nodes for child in (node, *node.iterdescendants())
                                  if isinstance(child.tag, str) and re.fullmatch(r'h[2-6]', child.tag)),
             'empty': _cf_empty_block(nodes)}
            for kind, _, nodes in blocks
        ],
        'legacy_sections_count': len(legacy),
        'legacy_sections_complete': legacy_complete,
        'article': True,
        'heading_counts': dict(Counter(node.tag for node in headings)),
        'classified_counts': dict(kinds),
        'toc_count': len(toc),
        'toc_missing_count': len(toc - parsed),
        'table_count': len(root.xpath('.//table')),
        'explicitly_empty': _cf_explicitly_empty(root),
        'category_like_heading_count': sum(bool(re.search(
            r'\b(?:mandatory|required|recommended)\b', text_content(node), re.I,
        )) for node in headings),
    }
    # Only fixed vocabulary, structural tags and counts are safe for the
    # aggregate Runner diagnostic. Never emit source titles, URLs or bug IDs.
    category_words = ('mandatory', 'required', 'strongly', 'recommended',
                      'optional', 'functional', 'security', 'critical',
                      'patch', 'patches', 'fix', 'fixes', 'update', 'updates',
                      'bug', 'bugs', 'applicable', 'non', 'and', 'or',
                      'android', 'for', 'on', 'in', 'to', 'all', 'devices', 'device',
                      'version', 'versions', 'release', 'releases', 'later', 'above',
                      'there', 'are', 'were', 'is', 'was', 'no', 'none', 'zero', 'not',
                      'this', 'month', 'bulletin', 'the', 'current', 'available', 'new',
                      'issue', 'issues', 'id', 'ids', 'a', 'p', 'r', 'qpr')
    shapes = Counter()
    for node in [*headings, *root.xpath('.//p[strong or b]|.//div[strong or b]')]:
        label = text_content(node).casefold()
        if not re.search(r'\b(?:mandatory|required|recommended|optional)\b', label):
            continue
        if _cf_heading_match(label.upper()):
            continue
        tokens = re.findall(r'[a-z]+|\d+', label)
        signature = ' '.join(word if word in category_words else '#' if word.isdigit() else '*'
                             for word in tokens[:24])
        shapes[(node.tag, signature, bool(_cf_category(label)))] += 1
    report['category_shapes'] = [
        {'tag': tag, 'shape': shape, 'recognized': recognized, 'count': count}
        for (tag, shape, recognized), count in sorted(shapes.items())
    ][:30]
    # Count unknown heading and empty-notice forms using only a fixed,
    # non-source vocabulary. Numeric values and all other text are masked.
    def signature(node):
        tokens = re.findall(r'[a-z]+|\d+', text_content(node).casefold())
        return ' '.join(word if word in category_words else '#' if word.isdigit() else '*'
                        for word in tokens[:24])

    other = Counter((node.tag, signature(node)) for node in headings
                    if not _cf_category(text_content(node)) and not _cf_heading_match(text_content(node)))
    notices = Counter(signature(node) for node in root.xpath('.//p|.//li|.//h2|.//h3|.//h4')
                      if re.search(r'\b(?:no|none|zero|not)\b', text_content(node), re.I))
    report['other_heading_shapes'] = [
        {'tag': tag, 'shape': shape, 'count': count}
        for (tag, shape), count in sorted(other.items())
    ][:40]
    report['notice_shapes'] = [
        {'shape': shape, 'count': count} for shape, count in sorted(notices.items())
    ][:30]
    report['unparsed_heading_id_count'] = sum(
        bool(re.search(r'\b[APR]-\d+\b', text_content(node))) and
        not _cf_heading_match(text_content(node)) for node in headings
    )
    report['table_rows_with_patch_id'] = sum(
        bool(re.search(r'\b[APR]-\d+\b', text_content(row)))
        for row in root.xpath('.//table//tr')
    )
    report['empty_notice_candidates'] = sum(
        bool(re.search(r'\b(?:no|none|zero|not)\b', text_content(node), re.I)
             and re.search(r'\b(?:patches|fixes|updates)\b', text_content(node), re.I))
        for node in root.xpath('.//p|.//li')
    )
    if show_labels:
        report['local_heading_labels'] = [
            {'tag': node.tag, 'label': text_content(node)[:250], 'category': _cf_category(text_content(node))}
            for node in headings if not _cf_heading_match(text_content(node))
        ][:60]
        report['local_no_fix_notices'] = [
            text_content(node)[:250] for node in root.xpath('.//p|.//li')
            if re.search(r'\bno\b.*\b(?:fixes|patches|updates)\b', text_content(node), re.I)
        ][:20]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DB_PATH)
    parser.add_argument('--browser', choices=('auto', 'firefox', 'chromium'), default='auto')
    parser.add_argument('--cookie-file', type=Path)
    parser.add_argument('--cookie-header', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--show-local-labels', action='store_true')
    args = parser.parse_args()
    args.verbose = False
    source = next(item for item in SOURCES if item.key == 'gms_bulletins')
    with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        urls = [row[0] for row in conn.execute(
            "SELECT url FROM gms_update_bulletin_months WHERE status!='ready' ORDER BY url"
        )]
    with build_session(args) as session:
        for url in urls:
            found = _cf_month_link(source.url, url)
            if not found:
                print(json.dumps({'month': 'invalid stored URL', 'status': 'skipped'}))
                continue
            target, year, month = found
            status, final, body = fetch_html(session, target, args.timeout)
            final_month = _cf_month_link(source.url, final)
            report = {'month': f'{year} {month}', 'http_status': status}
            if status == 200 and body and final_month and final_month[1:] == (year, month):
                try:
                    report.update(describe(html.fromstring(body), final, show_labels=args.show_local_labels))
                except (ValueError, TypeError):
                    report['error'] = 'invalid HTML'
            else:
                report['redirected'] = urlsplit(final).path != urlsplit(target).path
            print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
