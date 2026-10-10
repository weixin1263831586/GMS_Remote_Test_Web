import subprocess
import unittest
from unittest.mock import patch

from lxml import html

from features.system.update_monitor import api_support
from features.system.update_monitor.fetching import fetch_source
from features.system.update_monitor.models import FetchedDocument, SourceConfig
from features.system.update_monitor.parsers import (
    _filter_recent_mainline,
    parse_gms_downloads,
)


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

        with self.assertRaisesRegex(RuntimeError, "Firefox.*HTTP 404|HTTP 404.*Firefox"):
            fetch_source(_Session(_Response(404)), source, 30)

    def test_sync_status_includes_last_stderr_error(self):
        result = subprocess.CompletedProcess(
            args=["python", "-m", "features.system.update_monitor.cli"],
            returncode=1,
            stdout="",
            stderr="fetching mainline_preload\nerror: Partner login expired\n",
        )
        with patch("features.system.update_monitor.api_support.subprocess.run", return_value=result):
            api_support._run_sync_job("full", ["mainline_preload"])

        with api_support._sync_lock:
            status = dict(api_support._sync_status)
        self.assertEqual(
            status["error"],
            "sync exited with 1: Partner login expired",
        )
        self.assertEqual(status["stderr"], result.stderr)


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
