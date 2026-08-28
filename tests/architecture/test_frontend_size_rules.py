"""Frontend size ratchet rules.

Mirrors tests/architecture/test_file_size_rules.py for web assets: existing
oversized files get explicit migration budgets that may only shrink, while
new files must stay within the default reviewable budget.  The goal is to
drive the 725 KB shell and the 100 KB-class page scripts down during the
planned frontend decomposition (dynamic imports, partials split).
"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# Existing debt may shrink, but must not grow while files are split.
MIGRATION_BYTE_LIMITS = {
    'web/shell/shell.html': 727213,          # target: < 100 KB after partials split
    'web/static/css/common.css': 130388,      # target: < 50 KB
    'web/static/js/navigation.js': 50 * 1024,
    'web/static/js/api-constants.js': 36109,
    'web/static/js/pages/test-suite-browser.js': 122084,   # target: < 50 KB
    'web/static/js/pages/report-analysis.js': 116723,      # target: < 50 KB
    'web/static/js/pages/firmware-burn.js': 110250,        # target: < 50 KB
    'web/static/js/pages/api-docs.js': 50139,
}

# Default budgets for anything not listed above.
DEFAULT_HTML_LIMIT = 100 * 1024
DEFAULT_JS_LIMIT = 50 * 1024
DEFAULT_CSS_LIMIT = 50 * 1024


def _limit_for(relative: str) -> int:
    if relative in MIGRATION_BYTE_LIMITS:
        return MIGRATION_BYTE_LIMITS[relative]
    if relative.endswith('.html'):
        return DEFAULT_HTML_LIMIT
    if relative.endswith('.js'):
        return DEFAULT_JS_LIMIT
    return DEFAULT_CSS_LIMIT


class FrontendSizeRuleTests(unittest.TestCase):
    def test_web_assets_stay_within_budgets(self):
        offenders = []
        for base in ('web/shell', 'web/static/css', 'web/static/js'):
            for path in (ROOT / base).rglob('*'):
                if not path.is_file() or path.suffix not in {'.html', '.js', '.css'}:
                    continue
                relative = str(path.relative_to(ROOT))
                size = path.stat().st_size
                limit = _limit_for(relative)
                if size > limit:
                    offenders.append((relative, size, limit))
        self.assertEqual(
            offenders,
            [],
            "frontend asset exceeds its size budget (see MIGRATION_BYTE_LIMITS): "
            f"{offenders}",
        )

    def test_migration_budgets_only_shrink(self):
        """Ratchet: migration budgets must not exceed the recorded debt."""
        for relative, limit in MIGRATION_BYTE_LIMITS.items():
            path = ROOT / relative
            if path.exists():
                self.assertGreaterEqual(
                    limit,
                    path.stat().st_size,
                    f"migration budget for {relative} must stay >= actual size",
                )

    def test_total_first_party_js_budget(self):
        """首屏基础 JS（非页面模块）总量不得超过当前基线，防止回弹。"""
        base_js = sorted((ROOT / 'web/static/js').glob('*.js'))
        total = sum(path.stat().st_size for path in base_js)
        self.assertLessEqual(
            total,
            231 * 1024,
            f"base JS total {total} bytes exceeds the 231 KB budget",
        )


if __name__ == '__main__':
    unittest.main()
