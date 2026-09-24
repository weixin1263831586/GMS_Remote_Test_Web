"""Frontend size ratchet rules.

Mirrors tests/architecture/test_file_size_rules.py for web assets: existing
oversized files get explicit migration budgets that may only shrink, while
new files must stay within the default reviewable budget.  The goal is to
drive the 725 KB shell and the 100 KB-class page scripts down during the
planned frontend decomposition (dynamic imports, partials split).

Ceilings 独立持久化在 ``baselines/frontend_size.json``，ratchet 语义与
Python 侧一致：相对 HEAD 只减不增，上调必须带 waivers（reason+expiry）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _size_ratchet import assert_no_ceiling_raises, load_baseline


ROOT = Path(__file__).resolve().parents[2]
BASELINE_NAME = "frontend_size.json"


def _byte_limits() -> dict[str, int]:
    return load_baseline(BASELINE_NAME)["ceilings"]

# Default budgets for anything not listed above.
DEFAULT_HTML_LIMIT = 100 * 1024
DEFAULT_JS_LIMIT = 50 * 1024
DEFAULT_CSS_LIMIT = 50 * 1024


def _limit_for(relative: str) -> int:
    limits = _byte_limits()
    if relative in limits:
        return limits[relative]
    if relative.endswith('.html'):
        return DEFAULT_HTML_LIMIT
    if relative.endswith('.js'):
        return DEFAULT_JS_LIMIT
    return DEFAULT_CSS_LIMIT


class FrontendSizeRuleTests(unittest.TestCase):
    @staticmethod
    def _scanned_assets():
        """全部第一方前端资产：web 三个根 + features/*/ui（评审 P2）。

        此前只扫 web/shell 与 web/static，页面模块化到 features/*/ui
        后（如 features/redmine/ui/page.js ≈ 244KB）完全脱离门禁——
        结果等价于绕过 size ratchet。这里把 features/*/ui 下的
        js/css/html 一并纳入同一 budget 体系。
        """
        for base in ('web/shell', 'web/static/css', 'web/static/js'):
            yield from (
                path for path in (ROOT / base).rglob('*')
                if path.is_file() and path.suffix in {'.html', '.js', '.css'}
            )
        for ui_dir in sorted((ROOT / 'features').glob('*/ui')):
            yield from (
                path for path in ui_dir.rglob('*')
                if path.is_file() and path.suffix in {'.html', '.js', '.css'}
            )

    def test_web_assets_stay_within_budgets(self):
        offenders = []
        for path in self._scanned_assets():
            relative = str(path.relative_to(ROOT))
            size = path.stat().st_size
            limit = _limit_for(relative)
            if size > limit:
                offenders.append((relative, size, limit))
        self.assertEqual(
            offenders,
            [],
            "frontend asset exceeds its size budget "
            "(baselines/frontend_size.json): "
            f"{offenders}",
        )

    def test_baseline_ratchet_no_ceiling_raises(self):
        """相对 HEAD，byte ceiling 不允许上调/新增，除非 waivers 豁免。"""
        assert_no_ceiling_raises(BASELINE_NAME)

    def test_registered_waivers_are_not_expired(self):
        from datetime import date

        data = load_baseline(BASELINE_NAME)
        today = date.today()
        expired = []
        for key, waiver in (data.get("waivers") or {}).items():
            expires_raw = str(waiver.get("expires") or "")
            try:
                expires = date.fromisoformat(expires_raw[:10])
            except ValueError:
                expired.append((key, f"unparseable expires {expires_raw!r}"))
                continue
            if expires < today:
                expired.append((key, f"expired {expires.isoformat()}"))
        self.assertEqual(expired, [], f"expired byte-budget waivers: {expired}")

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
