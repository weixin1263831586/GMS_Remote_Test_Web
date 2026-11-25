"""Critical Fix bulletin classification, coverage and detail parsing."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import html

from ..fetching import fetch_html
from ..models import (
    BulletinPatchRecord,
    FetchedDocument,
    ParsedSource,
)
from ..parsing import (
    MONTH_LOOKUP,
    heading_level,
    node_links,
    normalize_key,
    stable_hash,
    table_rows,
    text_content,
    utc_now,
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
            header_values, rows = table_rows(table, '')
            headers = [value.strip().lower() for value in header_values]
            if not headers or not headers[0].startswith('android version'):
                continue
            for row in rows:
                cells = row['values']
                if not cells:
                    continue
                version = _normalize_bulletin_version(cells[0])
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
        'bulletin_url', 'patch_kind', 'android_versions', 'detail_text', 'patch_rows_json',
    )})


_CF_MONTH_PATH = re.compile(r'/partners/releases/cf/([a-z]+)-(\d{4})/?$', re.IGNORECASE)
_CF_PATCH_HEADING = re.compile(r'^(?P<id>[APR]-\d+)\b\s*[:–—-]?\s*(?P<description>.*)$')


def _cf_month_link(base_url: str, href: str) -> tuple[str, str, str] | None:
    try:
        target = urlsplit(urljoin(base_url, href))
        base = urlsplit(base_url)
    except ValueError:
        return None
    if target.scheme != 'https' or target.netloc != base.netloc:
        return None
    match = _CF_MONTH_PATH.fullmatch(target.path)
    if not match:
        return None
    month = MONTH_LOOKUP.get(match.group(1).lower())
    if not month:
        return None
    return urlunsplit((target.scheme, target.netloc, target.path.rstrip('/'), '', '')), match.group(2), month[1]


def bulletin_month_identity(url: str) -> tuple[str, str, str] | None:
    found = _cf_month_link(url, url)
    if found:
        return urlsplit(found[0]).netloc, found[1], found[2]
    return None


def _cf_category(title: str) -> str:
    """Keep historical recommendation levels distinct; allow explicit scope suffixes."""
    label = re.sub(r'\s+', ' ', title.casefold()).strip(' :.')
    noun = r'(?:(?:functional|security|critical|bug)\s+)*(?:patch(?:es)?|fix(?:es)?|updates?)'
    # Historical headings may append Android release numbers or other scope
    # information after the explicit category + patch noun. Never infer a
    # category from an unqualified heading or from the absence of a deadline.
    for marker, kind in ((r'strongly\s+recommended', 'strongly_recommended'),
                         (r'mandatory|required', 'mandatory'),
                         (r'recommended', 'recommended')):
        if re.fullmatch(rf'(?:{marker})(?:\s+{noun})?', label):
            return kind
        if re.fullmatch(rf'{noun}\s*(?:\((?:{marker})\)|[-–—:]\s*(?:{marker}))', label):
            return kind
        scoped = re.fullmatch(rf'(?:{marker})\s+{noun}\b\s+(.+)', label)
        if scoped:
            suffix = scoped[1].strip(' ()[]:–—-')
            # An explicit leading category is evidence. Reject compound or
            # negated categories instead of incorrectly assigning their rows.
            if suffix and not re.search(
                r'\b(?:not|non|no|except|unless|mandatory|required|recommended|optional|versus|vs)\b|[?;]',
                suffix,
            ):
                return kind
    # Some historical bulletins explicitly group fixes under a functional
    # patch heading without stating either mandatory or recommendation level.
    # Preserve that distinction; the source heading is kept with the record.
    functional = re.fullmatch(
        r'(?:(?P<modifier>[a-z]+)\s+)?functional\s+patch(?:es)?'
        r'(?:\s+android\s+[\d\w., /()-]+)?', label,
    )
    if functional and functional['modifier'] not in {
        'no', 'zero', 'not', 'non', 'mandatory', 'required', 'recommended',
    }:
        return 'functional_unspecified'
    return ''


def _cf_heading_match(title: str):
    normalized = title.strip()
    normalized = re.sub(r'^(?:bug(?:\s+id)?|issue(?:\s+id)?|patch(?:\s+id)?)\s*:\s*', '', normalized, flags=re.I)
    # Older bulletins can separate the explicit A/P/R prefix and number
    # with whitespace or a typographic dash rather than an ASCII hyphen.
    normalized = re.sub(r'^([APR])(?:\s*[-‐‑‒–—−]\s*|\s+)(\d+)\b',
                        r'\1-\2', normalized)
    direct = _CF_PATCH_HEADING.match(normalized)
    if direct:
        return direct
    suffix = re.fullmatch(r'(.+?)\s*\(([APR]-\d+)\)\s*', normalized)
    return _CF_PATCH_HEADING.match(f'{suffix[2]} {suffix[1]}') if suffix else None


def _cf_explicitly_empty(root) -> bool:
    """Require a page-wide no-fix notice or explicit zero for both categories."""
    prefix = r'(?:there (?:are|were|is|was) no|no)'
    suffix = r'(?:\s+(?:for|in)\s+(?:this|the current)\s+(?:month|bulletin|release))?'
    noun = r'(?:(?:functional|critical|security|bug)\s+)*(?:patches|fixes|updates)'
    notices = [text_content(node).strip() for node in root.xpath('.//p|.//li|.//h2|.//h3|.//h4')]
    empty_categories = set()
    for notice in notices:
        # Exclude a security-only or other narrower category from whole-page proof.
        if re.fullmatch(rf'{prefix}\s+(?:(?:functional|critical)\s+)*(?:patches|fixes){suffix}[.!]?', notice, re.I):
            return True
        for marker, kind in ((r'mandatory|required', 'mandatory'),
                             (r'strongly\s+recommended', 'strongly_recommended')):
            if re.fullmatch(rf'{prefix}\s+(?:{marker})\s+{noun}{suffix}[.!]?', notice, re.I):
                empty_categories.add(kind)
    if empty_categories == {'mandatory', 'strongly_recommended'}:
        return True
    # In early bulletins each individual category may simply say "None".
    # Require every category section to contain only an explicit empty
    # declaration, not a missing body or an unrelated notice elsewhere.
    blocks = list(_cf_category_blocks(_cf_content_root(root)))
    return bool(blocks and any(kind == 'mandatory' for kind, _, _ in blocks)
                and any(kind in {'recommended', 'strongly_recommended'} for kind, _, _ in blocks)
                and all(_cf_empty_block(nodes) for _, _, nodes in blocks)
                and not any(_cf_heading_match(text_content(h))
                            for h in root.xpath('.//h2|.//h3|.//h4|.//h5|.//h6')))


def _cf_content_root(article):
    """Use the documentation body, excluding site feedback/footer widgets."""
    bodies = article.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " devsite-article-body ")]')
    return bodies[0] if len(bodies) == 1 else article


def _cf_nodes_between(elements, positions, heading, end):
    covered = {heading, *heading.iterdescendants()}
    nodes = []
    for node in elements[positions[heading] + 1:end]:
        if node in covered or not isinstance(node.tag, str):
            continue
        descendants = list(node.iterdescendants())
        if any(positions[child] >= end for child in descendants):
            continue
        nodes.append(node)
        covered.update(descendants)
    return nodes


def _cf_category_blocks(root):
    """Scope each explicit category to the next same/higher-level heading."""
    elements = list(root.iter())
    positions = {node: i for i, node in enumerate(elements)}
    headings = root.xpath('.//h2|.//h3|.//h4|.//h5|.//h6')
    for i, heading in enumerate(headings):
        kind = _cf_category(text_content(heading))
        if not kind:
            continue
        level = heading_level(heading)
        end = next((positions[node] for node in headings[i + 1:]
                    if heading_level(node) <= level or _cf_category(text_content(node))), len(elements))
        yield kind, heading, _cf_nodes_between(elements, positions, heading, end)


def _cf_empty_block(nodes) -> bool:
    text = ' '.join(text_content(node) for node in nodes if text_content(node)).strip()
    return bool(re.fullmatch(r'(?:none|no (?:functional )?(?:patches|fixes)(?: this month)?)[.!]?', text, re.I)
                and not any(node.xpath('.//a[@href]') or node.tag in ('a', 'table') for node in nodes))


_CF_LEGACY_SECTION = re.compile(r'^(?P<id>)(?P<description>.+)$')


def _cf_legacy_sections(root):
    """Index actual named legacy sections, without inventing Google bug IDs.

    Only used when a bulletin has no standard patch headings. Each record is
    a source section, not a claim that the section is one individual fix.
    Empty or ambiguous category blocks are not considered complete.
    """
    root = _cf_content_root(root)
    elements = list(root.iter())
    positions = {node: i for i, node in enumerate(elements)}
    blocks = list(_cf_category_blocks(root))
    records = []
    if not blocks:
        return records, False
    for kind, category, nodes in blocks:
        if _cf_empty_block(nodes):
            continue
        level = heading_level(category)
        candidates = [node for node in elements
                      if isinstance(node.tag, str) and node is not category and heading_level(node) == level + 1
                      and any(node is parent or parent in node.iterancestors() for parent in nodes)]
        if not candidates:
            return [], False
        scope = {node for parent in nodes for node in (parent, *parent.iterdescendants())}
        stop = max(positions[node] for node in scope) + 1
        # Preserve category introductions and nested groups rather than
        # interpreting them as independent numbered patches. Where a legacy
        # block cannot be split cleanly, index the complete category.
        prefix = _cf_nodes_between(elements, positions, category, positions[candidates[0]])
        if any(text_content(node) or node.xpath('.//a[@href]') for node in prefix):
            match = _CF_LEGACY_SECTION.fullmatch(text_content(category))
            if not match:
                return [], False
            records.append((kind, match, category, nodes))
            continue
        block_records = []
        for index, heading in enumerate(candidates):
            end = positions[candidates[index + 1]] if index + 1 < len(candidates) else stop
            body = _cf_nodes_between(elements, positions, heading, end)
            title = text_content(heading)
            if not title or not any(text_content(node) or node.xpath('.//a[@href]') for node in body):
                # The heading may introduce the next child group rather than
                # a separate fix. Keep its whole original category instead.
                match = _CF_LEGACY_SECTION.fullmatch(text_content(category))
                if not match:
                    return [], False
                block_records = [(kind, match, category, nodes)]
                break
            match = _CF_LEGACY_SECTION.fullmatch(title)
            if not match:
                return [], False
            block_records.append((kind, match, heading, body))
        records.extend(block_records)
    return records, bool(records)



def _cf_month_sections(root, toc_headings=None):
    """Discover bulletin entries independently of the site's heading depth."""
    kind = ''
    category_level = 0
    elements = list(root.iter())
    positions = {node: index for index, node in enumerate(elements)}
    boundaries = []
    toc_headings = toc_headings or {}
    candidates = root.xpath('.//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::h6 '
                            'or (self::p and (strong or b))]')
    for heading in candidates:
        title = text_content(heading).strip()
        level = heading_level(heading)
        if heading.tag == 'p':
            # Only accept a standalone bold label, not a body sentence mentioning a bug.
            bold = heading.xpath('./strong|./b')
            if len(bold) != 1 or text_content(bold[0]).strip() != title:
                continue
            level = category_level + 1 if category_level else 3
        match = _cf_heading_match(title)
        if not match and heading.get('id') in toc_headings:
            # A same-document TOC link may supply the explicit ID omitted from
            # the visible heading. Do not infer IDs from arbitrary body text.
            match = _cf_heading_match(toc_headings[heading.get('id')])
        if match:
            boundaries.append((heading, kind, match))
            continue
        category = _cf_category(title)
        if category:
            kind, category_level = category, level
        elif category_level and level <= category_level:
            kind, category_level = '', 0
        else:
            continue
        boundaries.append((heading, '', None))

    for index, (heading, kind, match) in enumerate(boundaries):
        if not match:
            continue
        end = positions[boundaries[index + 1][0]] if index + 1 < len(boundaries) else len(elements)
        yield kind, match, heading, _cf_nodes_between(elements, positions, heading, end)


def _cf_toc_entries(doc, month_url: str):
    """Yield same-document fragment links explicitly naming a patch."""
    month = urlsplit(month_url)
    for anchor in doc.xpath('//a[@href]'):
        target = urlsplit(urljoin(month_url, anchor.get('href') or ''))
        if not target.fragment or (target.scheme, target.netloc, target.path.rstrip('/')) != (
            month.scheme, month.netloc, month.path.rstrip('/'),
        ):
            continue
        title = text_content(anchor).strip()
        match = _cf_heading_match(title)
        if match:
            yield target.fragment, title, match.group('id')


def _cf_toc_headings(doc, month_url: str) -> dict[str, str]:
    by_anchor = {}
    conflicts = set()
    for fragment, title, patch_id in _cf_toc_entries(doc, month_url):
        if fragment in by_anchor and by_anchor[fragment][1] != patch_id:
            conflicts.add(fragment)
        by_anchor[fragment] = (title, patch_id)
    return {fragment: item[0] for fragment, item in by_anchor.items() if fragment not in conflicts}


def _cf_toc_patch_ids(doc, month_url: str) -> set[str]:
    return {patch_id for _, _, patch_id in _cf_toc_entries(doc, month_url)}


def _cf_patch_rows(nodes, base_url: str) -> list[dict[str, str]]:
    """Preserve version, subject, project and commit links from the same DOM row."""
    result = []
    for node in nodes:
        tables = [node] if node.tag == 'table' else node.xpath('.//table')
        for table in tables:
            headers, rows = table_rows(table, base_url)
            names = [re.sub(r'\s+', ' ', value).strip().lower() for value in headers]
            if not all(name in names for name in ('android version', 'subject', 'project')):
                continue
            vi, si, pi = (names.index(name) for name in ('android version', 'subject', 'project'))
            for row in rows:
                values = row['values']
                if len(values) <= max(vi, si, pi):
                    continue
                version = _normalize_bulletin_version(values[vi])
                if not version:
                    continue
                urls = [link['url'] for link in row['links']]
                result.append({'version': version, 'subject': values[si], 'project': values[pi],
                               'urls': urls})
    return result


def parse_gms_bulletins(fetched: FetchedDocument, session=None, *, timeout: float = 30.0) -> ParsedSource:
    """Index all monthly Critical Fix entries; use overview only for deadlines.

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
                    patch_match = re.match(r'([APR]-\d+)\s*(.*)', cells[0])
                    if not patch_match:
                        continue
                    patch_id, description = patch_match.group(1), patch_match.group(2)
                    # Bulletin 列的链接 → 月度详情页（#锚点为补丁 ID 数字）。
                    month_link = row.xpath('.//td//a[@href]')
                    bulletin_url = ''
                    for link_el in month_link:
                        href = link_el.get('href') or ''
                        if _cf_month_link(fetched.final_url, href):
                            bulletin_url = urljoin(fetched.final_url, href)
                            break
                    month_link_info = _cf_month_link(fetched.final_url, bulletin_url) if bulletin_url else None
                    bulletin_month = month_link_info[2] if month_link_info else cells[1]
                    payload = {
                        'source_key': fetched.source.key,
                        'year': year,
                        'patch_id': patch_id,
                        'description': description,
                        'bulletin_month': bulletin_month,
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
                            bulletin_month=bulletin_month,
                            deadline=cells[2],
                            bulletin_url=bulletin_url,
                            content_hash=stable_hash(payload),
                        )
                    )

    # Navigation also contains months without an overview deadline entry.
    months: dict[tuple[str, str], str] = {}
    for href in fetched.doc.xpath('//a[@href]/@href'):
        found = _cf_month_link(fetched.final_url, href)
        if found:
            url, year, month = found
            months.setdefault((year, month), url)

    overview = {patch.item_key: patch for patch in patches}
    by_key = dict(overview)
    ready_keys: set[str] = set()
    month_status: dict[str, str] = {}
    month_diagnostics: dict[str, str] = {}
    for (year, month), detail_url in months.items():
        month_status[detail_url] = 'unavailable'
        if not session:
            month_diagnostics[detail_url] = 'no authenticated session'
            continue
        time.sleep(0.4)
        status, final_url, detail_text = fetch_html(session, detail_url, timeout)
        final_month = _cf_month_link(fetched.final_url, final_url)
        if status != 200:
            month_diagnostics[detail_url] = f'HTTP {status}' if status else 'request failed or timed out'
            continue
        if not final_month or final_month[1:] != (year, month):
            month_diagnostics[detail_url] = 'redirected outside expected bulletin month'
            continue
        if not detail_text:
            month_diagnostics[detail_url] = 'empty response'
            continue
        try:
            doc = html.fromstring(detail_text)
        except (ValueError, TypeError):
            month_diagnostics[detail_url] = 'invalid HTML'
            continue
        articles = doc.xpath('//article[contains(@class, "devsite-article")]')
        if not articles:
            month_diagnostics[detail_url] = 'bulletin article missing (possible authentication or page structure change)'
            continue
        title = doc.xpath('//meta[@property="og:title"]/@content') or doc.xpath('//title/text()') or articles[0].xpath('.//h1/text()')
        title_versions = _bulletin_title_versions(title[0] if title else '')
        found_keys: set[str] = set()
        found_ids: set[str] = set()
        unclassified: set[str] = set()
        sections = list(_cf_month_sections(articles[0], _cf_toc_headings(doc, detail_url)))
        legacy_complete = False
        if not sections:
            sections, legacy_complete = _cf_legacy_sections(articles[0])
        for kind, match, heading, nodes in sections:
            patch_id = match.group('id')
            base_key = normalize_key(fetched.source.key, year, patch_id)
            old = overview.get(base_key) if patch_id else None
            if patch_id:
                found_ids.add(patch_id)
            if old and old.bulletin_month.casefold() == month.casefold():
                key = base_key
                found_keys.add(key)
            else:
                # A later bulletin can repeat or extend an earlier fix.
                # Its source and version evidence belong to this month.
                key = (normalize_key(fetched.source.key, year, month, patch_id) if patch_id else
                       normalize_key(fetched.source.key, year, month, 'legacy-section',
                                     heading.get('id') or text_content(heading)))
                old = None
            if key in ready_keys:
                continue
            if kind == 'functional_unspecified' and old:
                kind = 'mandatory'
            if not kind:
                # The overview is the official mandatory deadline list. Only an
                # exact ID and month match can supply missing legacy classification.
                if old:
                    kind = 'mandatory'
                else:
                    unclassified.add(patch_id)
            versions = _bulletin_section_versions(nodes, title_versions)
            texts = [text_content(heading)]
            if kind == 'functional_unspecified':
                category_labels = [text_content(node) for node in heading.xpath('preceding::*[self::h2 or self::h3 or self::h4]')
                                   if _cf_category(text_content(node)) == kind]
                if category_labels:
                    texts.insert(0, category_labels[-1])
            for node in nodes:
                texts.append(text_content(node))
                texts.extend(link.url for link in node_links(node, detail_url))
            anchor = heading.get('id') or (patch_id.split('-', 1)[1] if patch_id else '')
            record = BulletinPatchRecord(
                source_key=fetched.source.key, item_key=key, year=year,
                patch_id=patch_id, patch_kind=kind,
                android_versions=', '.join(f'Android {v}' for v in versions),
                description=match.group('description') or (old.description if old else ''),
                bulletin_month=month, deadline=old.deadline if old else '',
                bulletin_url=(old.bulletin_url if old else '') or (f'{detail_url}#{anchor}' if anchor else detail_url),
                content_hash='', detail_text='\n'.join(text for text in texts if text),
                detail_status='ready', detail_fetched_at=utc_now(),
                patch_rows_json=json.dumps(_cf_patch_rows(nodes, detail_url), ensure_ascii=False),
            )
            by_key[key] = replace(record, content_hash=bulletin_content_hash(record))
            ready_keys.add(key)
        expected = {
            key for key, row in overview.items()
            if row.bulletin_url and bulletin_month_identity(row.bulletin_url) == bulletin_month_identity(detail_url)
        }
        toc_ids = _cf_toc_patch_ids(doc, detail_url)
        issues = []
        if not found_ids and not (not expected and not toc_ids and
                                  (legacy_complete or _cf_explicitly_empty(articles[0]))):
            issues.append('no patch headings parsed (check legacy layout or explicit no-fix notice)')
        missing_overview = sorted(overview[key].patch_id for key in expected - found_keys)
        if missing_overview:
            issues.append('overview missing: ' + ', '.join(missing_overview))
        if toc_ids - found_ids:
            issues.append('TOC missing: ' + ', '.join(sorted(toc_ids - found_ids)))
        if unclassified:
            issues.append('unclassified: ' + ', '.join(sorted(unclassified)))
        if issues:
            month_diagnostics[detail_url] = '; '.join(issues)
        else:
            month_status[detail_url] = 'ready'
    for key, patch in overview.items():
        if key not in ready_keys:
            record = replace(patch, detail_status='unavailable' if patch.bulletin_url else 'not_applicable')
            by_key[key] = replace(record, content_hash=bulletin_content_hash(record))
    return ParsedSource(bulletin_patches=list(by_key.values()), bulletin_month_status=month_status,
                        bulletin_month_diagnostics=month_diagnostics)
