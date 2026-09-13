"""CSP boundary tests.

Split from test_security_boundary.py after the file outgrew the
reviewable-size gate in tests/architecture/test_file_size_rules.py.
Covers the strict production CSP header and the dedicated noVNC proxy
policy; shares the production-bootstrap fixture via
SecurityBoundaryFixtureTests.
"""

from __future__ import annotations

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
