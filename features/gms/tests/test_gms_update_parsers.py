import unittest
from unittest.mock import patch

from lxml import html

from features.gms.fetching import fetch_source
from features.gms.models import FetchedDocument, SourceConfig
from features.gms.parsers.downloads import parse_gms_downloads
from features.gms.parsers.mainline import _filter_recent_mainline


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.url = "https://docs.partner.android.com/protected"
        self.content = b""

    def raise_for_status(self):
        raise AssertionError("protected HTTP errors should be translated first")


class _Session:
    def __init__(self, response):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


class UpdateMonitorFailureTests(unittest.TestCase):
    def test_protected_404_explains_partner_login_recovery(self):
        source = SourceConfig(
            key="mainline_preload",
            name="Mainline PRELOAD Release Notes",
            url="https://docs.partner.android.com/mainline/release/release-notes",
            category="mainline_package",
            parser="mainline_release_notes",
            auth_required=True,
        )

        # 认证失效路径现在会自动唤起主机 Firefox 登录页：单测里替换为
        # mock，只断言接线（真实唤起行为由 test_partner_auth_auto_login 覆盖）。
        with patch(
            "features.gms.fetching.fetch_partner_android.launch_login_browser"
        ) as launch, self.assertRaisesRegex(RuntimeError, "Firefox.*HTTP 404|HTTP 404.*Firefox"):
            fetch_source(_Session(_Response(404)), source, 30)
        launch.assert_called_once_with(
            source.url, reason=f"{source.name} HTTP 404"
        )

    def test_auth_optional_source_404_does_not_launch_browser(self):
        source = SourceConfig(
            key="cts_downloads",
            name="CTS Downloads",
            url="https://source.android.com/docs/compatibility/cts/downloads",
            category="test_suite",
            parser="test_suite_downloads",
            auth_required=False,
        )
        response = _Response(404)
        response.raise_for_status = lambda: None
        response.content = b"<html><body>moved</body></html>"

        with patch(
            "features.gms.fetching.fetch_partner_android.launch_login_browser"
        ) as launch:
            fetched = fetch_source(_Session(response), source, 30)

        launch.assert_not_called()
        self.assertEqual(fetched.status_code, 404)


class GmsBulletinParserTests(unittest.TestCase):
    def _parse(self, article_html: str, session=None):
        from lxml import html as lh

        from features.gms.models import FetchedDocument, SourceConfig
        from features.gms.parsers.critical import parse_gms_bulletins

        source = SourceConfig(
            key="gms_bulletins",
            name="Critical Fix Supplementary Bulletins",
            url="https://docs.partner.android.com/partners/releases/cf",
            category="gms_bulletin",
            parser="gms_bulletins",
            auth_required=True,
        )
        doc = lh.fromstring(f"<html><body><article class='devsite-article'>{article_html}</article></body></html>")
        fetched = FetchedDocument(
            source=source, doc=doc, title="CF", content_hash="x", status_code=200,
            final_url="https://docs.partner.android.com/partners/releases/cf",
        )
        return parse_gms_bulletins(fetched, session=session).bulletin_patches

    def test_parses_year_sections_links_and_patch_rows(self):
        patches = self._parse("""
          <h2>2026 bulletins</h2>
          <table>
            <tr><th>Mandatory patches</th><th>Bulletin</th><th>Deadline</th></tr>
            <tr><td>A-568181732 Fix LoopbackPassthroughTest assertion failure and platform audio policy deadlock</td><td><a href="/partners/releases/cf/nov-2026#568181732">November</a></td><td>March 26, 2027</td></tr>
            <tr><td>A-520314828 Skip redundant null RemoteViews update on app widget deletion</td><td>September</td><td>March 26, 2027</td></tr>
          </table>
          <h2>2025 bulletins</h2>
          <table>
            <tr><th>Mandatory patches</th><th>Bulletin</th><th>Deadline</th></tr>
            <tr><td>A-428733109 Enhanced Intent Firewall to inspect Intent extras</td><td><a href="https://docs.partner.android.com/partners/releases/cf/nov-2025#428733109">November</a></td><td>March 26, 2026</td></tr>
          </table>
        """)

        self.assertEqual(len(patches), 3)
        first = patches[0]
        self.assertEqual(first.year, "2026")
        self.assertEqual(first.patch_id, "A-568181732")
        self.assertIn("LoopbackPassthroughTest", first.description)
        self.assertEqual(first.bulletin_month, "November")
        self.assertEqual(first.deadline, "March 26, 2027")
        # Bulletin 单元格里的链接必须落到 bulletin_url（相对路径拼全）。
        self.assertEqual(
            first.bulletin_url,
            "https://docs.partner.android.com/partners/releases/cf/nov-2026#568181732",
        )
        self.assertEqual(
            patches[2].bulletin_url,
            "https://docs.partner.android.com/partners/releases/cf/nov-2025#428733109",
        )
        self.assertEqual(patches[1].bulletin_url, "")
        self.assertEqual({p.year for p in patches}, {"2025", "2026"})
        self.assertEqual(patches[-1].patch_id, "A-428733109")

    def test_ignores_rows_without_patch_id_and_unrelated_headings(self):
        patches = self._parse("""
          <h2>Overview</h2>
          <table><tr><td>no id row</td></tr></table>
          <h2>2026 bulletins</h2>
          <table>
            <tr><td><a href="https://x/A-111">A-111</a> linked id description</td><td>May</td><td>June 18, 2026</td></tr>
            <tr><td>A-222</td><td>June</td><td>July 1, 2026</td></tr>
          </table>
        """)

        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[0].patch_id, "A-111")
        self.assertIn("linked id description", patches[0].description)
        self.assertEqual(patches[0].item_key, patches[0].item_key)
        self.assertNotEqual(patches[0].item_key, patches[1].item_key)

    @staticmethod
    def _detail_session(detail_html: str):
        class _DetailResponse:
            status_code = 200
            url = "https://docs.partner.android.com/partners/releases/cf/nov-2026"
            text = detail_html

        class _DetailSession:
            def get(self, *_args, **_kwargs):
                return _DetailResponse()

        return _DetailSession()

    def test_enriches_all_versions_from_patch_section_table(self):
        # 详情页小节的 "Android version" 表逐行列出每个适用版本
        # （A-568181732 同时覆盖 16 / 16 QPR2 / 17，各有独立提交与下载）。
        detail_html = """
          <html><head><meta property="og:title" content="November 2026 Android Critical Patches Bulletin - Android 16, 16 QPR2, and 17 | Android Partners" /></head>
          <body><article class="devsite-article">
            <h2>Mandatory functional patches</h2>
            <h3>A-568181732 Fix LoopbackPassthroughTest assertion failure</h3>
            <p>On unpatched Android 16 system images, the corrected CTS test fails.</p>
            <table>
              <tr><th>Android version</th><th>Subject</th><th>Download</th></tr>
              <tr><td>16</td><td>fix A</td><td><a href="https://d/1">Download</a></td></tr>
              <tr><td>16 QPR2</td><td>fix B</td><td><a href="https://d/2">Download</a></td></tr>
              <tr><td>17</td><td>fix C</td><td><a href="https://d/3">Download</a></td></tr>
            </table>
          </article></body></html>
        """

        with patch("features.gms.parsers.critical.time.sleep"):
            patches = self._parse(
                """
                  <h2>2026 bulletins</h2>
                  <table>
                    <tr><th>Mandatory patches</th><th>Bulletin</th><th>Deadline</th></tr>
                    <tr><td>A-568181732 Fix LoopbackPassthroughTest assertion failure</td><td><a href="/partners/releases/cf/nov-2026#568181732">November</a></td><td>March 26, 2027</td></tr>
                  </table>
                """,
                session=self._detail_session(detail_html),
            )

        first = patches[0]
        self.assertEqual(first.patch_kind, "mandatory")
        self.assertEqual(
            first.android_versions,
            "Android 16, Android 16 QPR2, Android 17",
        )

    def test_bulletin_title_versions_covers_modern_and_legacy_formats(self):
        from features.gms.parsers.critical import _bulletin_title_versions

        # 新格式：标题枚举带 "Android" 前缀 + 不带前缀的后续版本。
        self.assertEqual(
            _bulletin_title_versions(
                "November 2026 Android Critical Patches Bulletin - Android 16, 16 QPR2, and 17 | Android Partners"
            ),
            ["16", "16 QPR2", "17"],
        )
        # 旧格式："Nov 2020 Update Bulletin (10 and 11)"。
        self.assertEqual(
            _bulletin_title_versions("Nov 2020 Update Bulletin (10 and 11) | Android Partners"),
            ["10", "11"],
        )
        self.assertEqual(_bulletin_title_versions("Some Unrelated Page Title"), [])

    def test_version_fallback_combines_body_text_and_title_scope(self):
        # 小节没有 "Android version" 表时，回退到正文 "Android N" 提法
        # 与页面标题枚举（"16 QPR2, and 17" 不带 Android 前缀）的并集。
        detail_html = """
          <html><head><meta property="og:title" content="November 2026 Android Critical Patches Bulletin - Android 16, 16 QPR2, and 17 | Android Partners" /></head>
          <body><article class="devsite-article">
            <h2>Strongly recommended functional patches</h2>
            <h3>A-123456789 Example patch</h3>
            <p>Applies to Android 16 devices only.</p>
          </article></body></html>
        """

        with patch("features.gms.parsers.critical.time.sleep"):
            patches = self._parse(
                """
                  <h2>2026 bulletins</h2>
                  <table>
                    <tr><th>Mandatory patches</th><th>Bulletin</th><th>Deadline</th></tr>
                    <tr><td>A-123456789 Example patch</td><td><a href="/partners/releases/cf/nov-2026#123456789">November</a></td><td>March 26, 2027</td></tr>
                  </table>
                """,
                session=self._detail_session(detail_html),
            )

        first = patches[0]
        self.assertEqual(first.patch_kind, "strongly_recommended")
        self.assertEqual(
            first.android_versions,
            "Android 16, Android 16 QPR2, Android 17",
        )


class MainlineMonthWindowTests(unittest.TestCase):
    def test_month_window_keeps_all_builds_within_depth_months(self):
        # 12-month window holding more than 12 builds (two in the oldest
        # month): every in-window entry must survive, the count is not a cap.
        entries = [
            (2026, 6, 'notes-PRELOAD-2026-06-11', 'https://e/11'),
            (2026, 6, 'notes-PRELOAD-2026-06-09', 'https://e/9'),
            *[
                (2025, 7, f'notes-PRELOAD-2025-07-{day:02d}', f'https://e/{day}')
                for day in range(1, 15)
            ],
            (2025, 6, 'notes-PRELOAD-2025-06-30', 'https://e/old'),
        ]

        kept = _filter_recent_mainline(entries, 12, now_year=2026, now_month=6)

        self.assertEqual(len(kept), 16)
        self.assertNotIn((2025, 6, 'notes-PRELOAD-2025-06-30', 'https://e/old'), kept)
        self.assertEqual(kept[0][2], 'notes-PRELOAD-2026-06-11')


class GmsRowspanTests(unittest.TestCase):
    def test_rowspan_continuation_row_reads_aligned_columns(self):
        table_html = """
        <article class="devsite-article">
          <h2>GMS packages</h2>
          <table>
            <thead><tr>
              <th>Android version</th><th>File</th><th>Release notes</th>
              <th>Description</th><th>Partner Gerrit tag</th>
              <th>Required for new IR builds seeking approvals from</th>
            </tr></thead>
            <tbody>
              <tr>
                <td rowspan="2">Android 16</td>
                <td><a href="https://example/gms-a.zip">gms-a.zip</a></td>
                <td><a href="https://example/notes-a">notes</a></td>
                <td>Description A</td>
                <td><a href="https://example/tag-a">tag-a</a></td>
                <td>2026-01-01</td>
              </tr>
              <tr>
                <td><a href="https://example/gms-b.zip">gms-b.zip</a></td>
                <td><a href="https://example/notes-b">notes</a></td>
                <td>Description B</td>
                <td><a href="https://example/tag-b">tag-b</a></td>
                <td>2026-02-01</td>
              </tr>
            </tbody>
          </table>
        </article>
        """
        source = SourceConfig(
            key='gms_downloads',
            name='GMS Downloads',
            url='https://docs.partner.android.com/gms',
            category='gms_package',
            parser='gms_downloads',
        )
        fetched = FetchedDocument(
            source=source,
            doc=html.fromstring(table_html),
            title='GMS',
            content_hash='x',
            status_code=200,
            final_url=source.url,
        )

        packages = parse_gms_downloads(fetched).gms_packages

        self.assertEqual(len(packages), 2)
        continuation = packages[1]
        self.assertEqual(continuation.android_version, 'Android 16')
        self.assertEqual(continuation.file_name, 'gms-b.zip')
        self.assertEqual(continuation.description, 'Description B')
        self.assertEqual(continuation.partner_gerrit_tag, 'tag-b')
        self.assertEqual(continuation.required_from, '2026-02-01')


if __name__ == "__main__":
    unittest.main()


class ScanCompletenessRegressionTests(unittest.TestCase):
    def test_same_text_changed_link_is_not_an_unchanged_document(self):
        from features.gms.parsing import stable_doc_hash

        first = html.fromstring('<article><a href="/old.zip">Download</a></article>')
        second = html.fromstring('<article><a href="/new.zip">Download</a></article>')
        self.assertNotEqual(stable_doc_hash(first), stable_doc_hash(second))

    def test_preload_missing_child_does_not_produce_empty_build(self):
        from features.gms.parsers.mainline import parse_mainline_release_notes

        source = SourceConfig('mainline_preload', 'PRELOAD', 'https://docs.partner.android.com/mainline/release/release-notes',
                              'mainline_package', 'mainline_release_notes')
        doc = html.fromstring('<article><a href="/mainline/release/release-notes/2026/nov/notes-PRELOAD-2026-11-01">Build</a></article>')
        fetched = FetchedDocument(source, doc, 'PRELOAD', 'hash', 200, source.url)
        with patch('features.gms.parsers.mainline.fetch_html', return_value=(403, source.url, '')), self.assertRaisesRegex(ValueError, 'existing records retained'):
            parse_mainline_release_notes(fetched, depth=0)


    def test_table_expands_version_and_project_rowspans(self):
        from features.gms.parsing import table_rows

        table = html.fromstring('<table><tr><th>Version</th><th>Subject</th><th>Project</th></tr>'
                                '<tr><td rowspan="2">16 QPR2</td><td>first</td><td rowspan="2">base</td></tr>'
                                '<tr><td>second</td></tr></table>')
        _headers, rows = table_rows(table, 'https://example.test/')
        self.assertEqual(rows[1]['values'], ['16 QPR2', 'second', 'base'])

    def test_requirement_versions_keep_qpr_and_future_releases(self):
        from features.gms.parsing import classify_requirement_versions

        self.assertIn(('Android 16 QPR2', 'specific'),
                      classify_requirement_versions('Applies to Android 16 QPR2'))
        self.assertIn(('Android 18', 'added'),
                      classify_requirement_versions('Start of requirements added in Android 18'))

    def test_explicit_requirement_marker_overrides_inherited_kind(self):
        from features.gms.parsing import classify_requirement_versions

        self.assertEqual(classify_requirement_versions(
            'Start of requirements changed in Android 17', 'added'),
            [('Android 17', 'changed')])
        self.assertEqual(classify_requirement_versions(
            'Start of requirements added in Android 16 QPR2', 'changed'),
            [('Android 16 QPR2', 'added')])
        self.assertEqual(classify_requirement_versions(
            'Applies to Android 16 and 17', 'changed'),
            [('Android 16', 'changed'), ('Android 17', 'changed')])


    def test_requirement_table_identity_survives_unrelated_insertions(self):
        from features.gms.parsers.requirements import parse_gms_requirements

        source = SourceConfig('gms_requirements', 'Requirements',
                              'https://docs.partner.android.com/gms/requirements',
                              'requirements', 'gms_requirements')
        existing = '<h2>2. Existing</h2><table><tr><th>Rule</th></tr><tr><td>Keep</td></tr><tr><td>Keep</td></tr></table>'
        inserted = '<h2>1. Earlier</h2><table><tr><th>Rule</th></tr><tr><td>New</td></tr></table>'
        def parse(body):
            doc = html.fromstring('<article class="devsite-article">' + body + '</article>')
            fetched = FetchedDocument(source, doc, 'Requirements', 'hash', 200, source.url)
            return parse_gms_requirements(fetched).requirement_table_rows
        original = parse(existing)
        subsequent = [r for r in parse(inserted + existing) if r.row_text == 'Keep']
        self.assertEqual(len(original), 2)
        self.assertEqual(len({r.row_key for r in original}), 2)
        self.assertEqual([r.row_key for r in original], [r.row_key for r in subsequent])
