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
    # CSP 前置迁移后 shell 内联脚本外置(shell-main.js 等),html 大幅收缩。
    'web/shell/shell.html': 262103,          # 728210→262073→262103: +30 残留 onkeypress 迁移为 data-keypress 委托; target: < 100 KB after partials split
    'web/static/css/common.css': 148115,      # +1092: inline hover 样式迁移为声明式 CSS; +2841: 9月 UI 对齐; +174: CSS 变量缺省回退(--bg-color 等)双主题兜底; target: < 50 KB after split
    'web/static/js/navigation.js': 50 * 1024,
    'web/static/js/api-constants.js': 36286,
    'web/static/js/pages/test-suite-browser.js': 125721,   # target: < 50 KB (browser context isolation + direct local fetch & request generation)
    'web/static/js/pages/report-analysis.js': 115880,      # -1265: KB/源码卡片渲染拆至 report-analysis-diagnosis.js（ADR 0014 背景分栏同文件新增）; target: < 50 KB
    'web/static/js/pages/firmware-burn.js': 129988,        # +529: beforeunload 拦截移入 try 外并在失败路径移除（防上传中断警告常驻）; target: < 50 KB
    'web/static/js/pages/api-docs.js': 50374,              # +165: act-bridge 委托 helper; +70: clipboard 仅安全上下文可用，失败回退 copyText
    # 拆分兑现（评审意见：不放宽 weekly-report 预算，优先拆分）：周报分析
    # 面板迁至 utility-tools.js（46.7 KB，默认限额内）；预算收缩到实际值。
    'web/static/js/shell/weekly-report.js': 41351,         # 83553→41351: 分析面板拆出; target: < 50 KB
    # 原 shell.html 内联主脚本外置(仅搬运,CSP 前置迁移);随 partials 拆分继续收缩。
    'web/static/js/shell/shell-main.js': 288220,           # +lazy activation 早退(set-username); +2084: workflow tabs 页头统一迁移; +4573: 9月 shell/终端对齐; target: < 100 KB after decomposition
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
