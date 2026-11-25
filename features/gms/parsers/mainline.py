"""Mainline PRELOAD, known issues and security patch parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from lxml import html

from ..fetching import (
    build_train_url,
    fetch_html,
    recent_month_cutoff,
)
from ..models import (
    FetchedDocument,
    MainlinePackageRecord,
    ParsedSource,
)
from ..parsing import (
    MONTH_INDEX_TO_KEY,
    MONTH_LOOKUP,
    heading_level,
    mainline_doc_hash,
    normalize_key,
    stable_hash,
    text_content,
)
from ..sources import SOURCES


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
        if dir_month not in MONTH_LOOKUP:
            continue
        if not _MAINLINE_SLUG_DATE_RE.search(slug):
            continue
        year = int(dir_year)
        month_index = MONTH_LOOKUP[dir_month][0]
        key = f'{year}|{month_index}|{slug}'
        if key in seen:
            continue
        notes_url = urljoin(base_url, href.split('?')[0] + '?authuser=2')
        if urlsplit(notes_url).netloc != urlsplit(base_url).netloc:
            continue
        seen.add(key)
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


def mainline_entries(fetched: FetchedDocument, depth=0):
    discovered = _extract_mainline_links(fetched.doc, fetched.final_url)
    return _filter_recent_mainline(discovered, depth) if depth else sorted(discovered, reverse=True)


def parse_mainline_package(fetched: FetchedDocument, entry, child_doc):
    year, month_index, slug, notes_url = entry
    month_key = MONTH_INDEX_TO_KEY.get(month_index, '')
    month_label = MONTH_LOOKUP.get(month_key, (month_index, ''))[1]
    build_id, label = _partner_zip_build_id(child_doc)
    if not build_id:
        raise ValueError(f'Mainline PRELOAD: missing partner zip evidence for {slug}; existing records retained')
    payload = {
        'source_key': fetched.source.key, 'year': str(year), 'month': month_key,
        'preload_version': slug, 'partner_zip_build_id': build_id,
        'notes_url': notes_url, 'partner_zip_label': label,
    }
    return MainlinePackageRecord(
        source_key=fetched.source.key,
        item_key=normalize_key(fetched.source.key,str(year),month_key,slug),
        year=str(year), month=month_key, month_label=month_label,
        preload_version=slug, notes_url=notes_url, partner_zip_build_id=build_id,
        ci_build_url=build_train_url(build_id), partner_zip_label=label,
        content_hash=stable_hash(payload),
    )


def parse_mainline_release_notes(fetched: FetchedDocument, session=None, *, timeout: float=30.0, depth: int|None=None) -> ParsedSource:
    """Standalone parser for document audits; the scanner shares fetched child DOMs."""
    entries=mainline_entries(fetched,0 if depth is None else depth)
    if not entries:
        raise ValueError('Mainline PRELOAD: no release links discovered; existing records retained')
    packages=[]
    for entry in entries:
        url=entry[3]
        status,final,text=fetch_html(session,url,timeout)
        if status!=200 or not text or urlsplit(final).path.rstrip('/')!=urlsplit(url).path.rstrip('/'):
            raise ValueError('Mainline PRELOAD: missing partner zip evidence; existing records retained')
        packages.append(parse_mainline_package(fetched,entry,html.fromstring(text)))
    return ParsedSource(mainline_packages=packages)


DEFAULT_INDEX_URL = next(source.url for source in SOURCES if source.key == 'mainline_preload')
KNOWN_ISSUE_HEADING_RE = re.compile(r'^(MTS|CTS|GTS)\s+known issues\b.*:$', flags=re.IGNORECASE)
PRODUCT_SECTIONS = ('Android', 'Android Go')


@dataclass(frozen=True)
class ReleasePage:
    year: int
    label: str
    url: str


@dataclass(frozen=True)
class FetchedPage:
    doc: html.HtmlElement
    content_hash: str
    status_code: int
    final_url: str


@dataclass(frozen=True)
class KnownIssue:
    source_url: str
    source_title: str
    release_year: int
    release_label: str
    product_section: str
    issue_type: str
    android_versions: str
    category: str
    test_module: str
    test_case: str
    exemption_id: str
    issue_text: str


@dataclass(frozen=True)
class SecurityPatch:
    source_url: str
    source_title: str
    release_year: int
    release_label: str
    product_section: str
    cve_id: str
    mainline_module: str
    version_note: str


def fetch_page(session: requests.Session, url: str, timeout: float) -> FetchedPage:
    response = session.get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    doc = html.fromstring(response.content)
    content_hash = mainline_doc_hash(doc)
    return FetchedPage(
        doc=doc,
        content_hash=content_hash,
        status_code=response.status_code,
        final_url=response.url,
    )


def fetch_doc(session: requests.Session, url: str, timeout: float) -> html.HtmlElement:
    return fetch_page(session, url, timeout).doc


def extract_release_years(index_doc: html.HtmlElement) -> list[int]:
    nav_nodes = index_doc.xpath('//nav[contains(@class, "devsite-book-nav")]')
    if not nav_nodes:
        raise RuntimeError('left navigation bar not found: devsite-book-nav')
    years: list[int] = []
    seen: set[int] = set()
    for node in nav_nodes[0].xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " devsite-nav-title-no-path ")]'
    ):
        match = re.fullmatch(r'(\d{4}) Build Release Notes', text_content(node))
        if not match:
            continue
        year = int(match.group(1))
        if year in seen:
            continue
        seen.add(year)
        years.append(year)
    return years


def extract_release_pages(index_doc: html.HtmlElement, index_url: str, year: int) -> list[ReleasePage]:
    nav_nodes = index_doc.xpath('//nav[contains(@class, "devsite-book-nav")]')
    if not nav_nodes:
        raise RuntimeError('left navigation bar not found: devsite-book-nav')
    year_title = f'{year} Build Release Notes'
    year_nodes = nav_nodes[0].xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " devsite-nav-title-no-path ") '
        'and normalize-space(.)=$title]',
        title=year_title,
    )
    if not year_nodes:
        raise RuntimeError(f'left navigation section not found: {year_title}')

    section = year_nodes[0].getparent().xpath('./ul[contains(@class, "devsite-nav-section")]')
    if not section:
        raise RuntimeError(f'left navigation section has no links: {year_title}')

    pages: list[ReleasePage] = []
    seen: set[str] = set()
    for link in section[0].xpath('.//a[@href]'):
        url = urldefrag(urljoin(index_url, link.get('href')))[0]
        if url in seen:
            continue
        seen.add(url)
        label = text_content(link) or url.rsplit('/', 1)[-1]
        pages.append(ReleasePage(year=year, label=label, url=url))
    return pages


def find_section_nodes(article: html.HtmlElement, heading_text: str) -> list[html.HtmlElement]:
    headings = article.xpath(
        './/*[self::h2 or self::h3 or self::h4 or self::h5 or self::h6][normalize-space(.)=$heading]',
        heading=heading_text,
    )
    if not headings:
        return []
    heading = headings[0]
    level = heading_level(heading)
    nodes: list[html.HtmlElement] = []
    for sibling in heading.itersiblings():
        sibling_level = heading_level(sibling)
        if sibling_level and sibling_level <= level:
            break
        nodes.append(sibling)
    return nodes


def find_known_issue_lists(section_nodes: list[html.HtmlElement]) -> list[tuple[str, html.HtmlElement]]:
    lists: list[tuple[str, html.HtmlElement]] = []
    for node in section_nodes:
        for strong in node.xpath('.//strong'):
            match = KNOWN_ISSUE_HEADING_RE.match(text_content(strong))
            if not match:
                continue
            parent = strong
            while parent is not None and parent.tag != 'li':
                parent = parent.getparent()
            if parent is None:
                continue
            lists.extend((match.group(1).upper(), issue_list) for issue_list in parent.xpath('./ul'))
    return lists


def parse_issue_item(
    item: html.HtmlElement,
    *,
    page: ReleasePage,
    title: str,
    product_section: str,
    issue_type: str,
) -> list[KnownIssue]:
    issue_text = text_content(item)
    if 'internal bug ref.' not in issue_text.lower():
        return []

    bug_match = re.search(r'internal bug ref\.\s*([0-9,\sand]+)', issue_text, flags=re.IGNORECASE)
    if not bug_match:
        return []
    exemption_ids = re.findall(r'\d+', bug_match.group(1))
    if not exemption_ids:
        return []

    prefix_match = re.match(r'\[([^\]]+)\](?:\[([^\]]+)\])?', issue_text)
    android_versions = prefix_match.group(1).strip() if prefix_match else ''
    category = prefix_match.group(2).strip() if prefix_match and prefix_match.group(2) else ''

    codes = [text_content(code) for code in item.xpath('./code|./p/code|./strong/code')]
    nested_codes = [text_content(code) for code in item.xpath('./ul//code')]
    if not codes:
        all_codes = [text_content(code) for code in item.xpath('.//code')]
        codes = [code for code in all_codes if '#' not in code]
        nested_codes = [code for code in all_codes if '#' in code]

    test_modules = [code for code in codes if code and '#' not in code]
    test_cases = [code for code in nested_codes if '#' in code]
    if not test_modules or not test_cases:
        return []

    issues: list[KnownIssue] = []
    for exemption_id in exemption_ids:
        for test_module in test_modules:
            for test_case in test_cases:
                issues.append(
                    KnownIssue(
                        source_url=page.url,
                        source_title=title,
                        release_year=page.year,
                        release_label=page.label,
                        product_section=product_section,
                        issue_type=issue_type,
                        android_versions=android_versions,
                        category=category,
                        test_module=test_module,
                        test_case=test_case,
                        exemption_id=exemption_id,
                        issue_text=issue_text,
                    )
                )
    return issues


def extract_known_issues(page: ReleasePage, doc: html.HtmlElement) -> list[KnownIssue]:
    title = ' '.join(doc.xpath('//title/text()')).strip()
    articles = doc.xpath('//article[contains(@class, "devsite-article")]')
    article = articles[0] if articles else doc
    issues: list[KnownIssue] = []

    for product_section in PRODUCT_SECTIONS:
        section_nodes = find_section_nodes(article, product_section)
        for issue_type, issue_list in find_known_issue_lists(section_nodes):
            for item in issue_list.xpath('./li'):
                issues.extend(
                    parse_issue_item(
                        item,
                        page=page,
                        title=title,
                        product_section=product_section,
                        issue_type=issue_type,
                    )
                )
    return issues


def patch_section_heading(article: html.HtmlElement) -> html.HtmlElement | None:
    """Find the ``Security patches in this Mainline release`` heading.

    The heading is normally an h2, but matching h2-h6 keeps the extractor
    resilient to devsite layout changes.
    """
    headings = article.xpath(
        './/*[self::h2 or self::h3 or self::h4 or self::h5 or self::h6]'
        '[contains(normalize-space(.), "Security patches in this Mainline release")]'
    )
    return headings[0] if headings else None


def extract_security_patches(page: ReleasePage, doc: html.HtmlElement) -> list[SecurityPatch]:
    """Parse the per-release security-patch table (CVE / module / version).

    The section holds one table whose rows carry a CVE id, the affected
    Mainline module and a free-text version note (sometimes empty). Rows
    without a recognizable CVE id are skipped instead of failing the scan.
    """
    title = ' '.join(doc.xpath('//title/text()')).strip()
    articles = doc.xpath('//article[contains(@class, "devsite-article")]')
    article = articles[0] if articles else doc
    heading = patch_section_heading(article)
    if heading is None:
        return []
    heading_lvl = heading_level(heading)

    section = ''
    patches: list[SecurityPatch] = []
    seen: set[tuple[str, str]] = set()
    for sibling in heading.itersiblings():
        level = heading_level(sibling)
        if level:
            if level <= heading_lvl:
                break
            section = text_content(sibling)
            continue
        for row in sibling.xpath('.//tr'):
            cells = [text_content(cell) for cell in row.xpath('./th|./td')]
            if len(cells) < 2:
                continue
            # The CVE column can contain several identifiers. Use the first
            # CVE-bearing cell, not a CVE reference in a later version note.
            cve_column = next((i for i, cell in enumerate(cells)
                               if re.search(r'\bCVE-\d{4}-\d{4,}\b', cell, re.I)), None)
            if cve_column is None or cve_column + 1 >= len(cells):
                continue
            cve_ids = dict.fromkeys(m.upper() for m in
                                   re.findall(r'\bCVE-\d{4}-\d{4,}\b', cells[cve_column], re.I))
            module = cells[cve_column + 1]
            note = ' | '.join(cells[cve_column + 2:])
            for cve_id in cve_ids:
                key = (cve_id, module)
                if key in seen:
                    continue
                seen.add(key)
                patches.append(
                    SecurityPatch(
                        source_url=page.url,
                        source_title=title,
                        release_year=page.year,
                        release_label=page.label,
                        product_section=section or 'Security patches in this Mainline release',
                        cve_id=cve_id,
                        mainline_module=module,
                        version_note=note,
                    )
                )
    return patches


def issue_dedupe_key(issue: KnownIssue) -> tuple[str, str, str, str, str, str]:
    return (
        issue.product_section,
        issue.issue_type,
        issue.test_module,
        issue.test_case,
        issue.exemption_id,
        issue.android_versions,
    )


def dedupe_issues(issues: list[KnownIssue], known_keys: set[tuple[str, str, str, str, str, str]]) -> list[KnownIssue]:
    unique: list[KnownIssue] = []
    for issue in issues:
        key = issue_dedupe_key(issue)
        if key in known_keys:
            continue
        known_keys.add(key)
        unique.append(issue)
    return unique
