"""CSP boundary tests.

Split from test_security_boundary.py after the file outgrew the
reviewable-size gate in tests/architecture/test_file_size_rules.py.
Covers the strict production CSP header and the dedicated noVNC proxy
policy; shares the production-bootstrap fixture via
SecurityBoundaryFixtureTests.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from features.test_execution import suites_api

from .test_security_boundary import SecurityBoundaryFixtureTests


class CspBoundaryTests(SecurityBoundaryFixtureTests):
    def test_novnc_proxy_pages_get_dedicated_csp(self):
        """noVNC 上游页自带 inline script；代理路径放宽，其余保持严格。"""
        strict = self.client.get("/api/auth/status")
        novnc = self.client.get("/novnc/vnc.html")
        self.assertIn("script-src 'self';", strict.headers["content-security-policy"])
        self.assertIn(
            "script-src 'self' 'unsafe-inline';",
            novnc.headers["content-security-policy"],
        )

    def test_inline_suite_html_uses_opaque_sandbox(self):
        class LocalConfigManager:
            def __init__(self, suites_path):
                self.suites_path = suites_path

            def load_config(self):
                return {"suites_path": self.suites_path, "ubuntu_user": "tester"}

            def get_ubuntu_user(self, _config):
                return "tester"

            def is_config_host_local(self, _config):
                return True

        self.assertEqual(self._setup_admin().status_code, 200)
        root = Path(self.tmp.name) / "GMS-Suite"
        tools = root / "android-cts" / "tools"
        report = root / "android-cts" / "results" / "run" / "report.html"
        report.parent.mkdir(parents=True)
        report.write_text("<script>document.title='report'</script>", encoding="utf-8")
        manager = LocalConfigManager(str(root))

        with patch.object(suites_api.runtime, "config_manager", manager):
            preview = self.client.get(
                "/api/test/suites/download",
                params={
                    "suite_path": str(tools),
                    "path": "results/run/report.html",
                    "inline": "true",
                },
            )
            download = self.client.get(
                "/api/test/suites/download",
                params={
                    "suite_path": str(tools),
                    "path": "results/run/report.html",
                },
            )

        preview_csp = preview.headers["content-security-policy"]
        self.assertEqual(preview.status_code, 200)
        self.assertIn("sandbox allow-scripts", preview_csp)
        self.assertNotIn("allow-same-origin", preview_csp)
        self.assertIn("connect-src 'none'", preview_csp)
        self.assertEqual(preview.headers["referrer-policy"], "no-referrer")
        self.assertIn("no-store", preview.headers["cache-control"])
        self.assertIn("script-src 'self'", download.headers["content-security-policy"])
        self.assertNotIn("sandbox", download.headers["content-security-policy"])
