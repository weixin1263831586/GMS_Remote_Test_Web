"""Inline 事件处理器棘轮（CSP 收紧前置门禁）。

生产 CSP（bootstrap/application.py）的 ``script-src`` 目前仍带
``'unsafe-inline'``——在 shell.html 与各嵌入式 UI 页面的 inline
handler 全部迁移到 addEventListener/事件委托之前无法移除。

本测试是收敛棘轮：每个文件的 inline handler 数量不得超过当前基线，
且已清零的文件不得回退。基线只减不增；全部归零后收紧 CSP（移除
script-src 的 unsafe-inline）并清空基线清单。

已归零（默认上限 0 防回退）：web/static/js 全部页面与 shell 模块——
经 act-bridge（web/static/js/shell/act-bridge.js）事件委托迁移。
剩余债务在嵌入式 UI 页面与 shell.html，由 EMBEDDED_BASELINE /
SHELL_BASELINE 锁定。
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 只匹配 HTML 属性形态的 inline handler（onxxx="..." / onxxx='...'）。
# JS 里的 ``el.onclick = ...`` 属性赋值不受 CSP 限制，不在此门禁范围。
# 事件名覆盖 act-bridge（web/static/js/shell/act-bridge.js）支持的全集，
# 防止新事件名（如 keypress/toggle）绕过门禁却仍触发 CSP。
INLINE_HANDLER_RE = re.compile(
    r"\bon(?:click|change|input|submit|keydown|keyup|keypress|load|error|"
    r"mouseover|mouseout|focus|blur|dblclick|dragstart|dragend|dragover|"
    r"dragenter|drop|toggle|contextmenu|scroll)\s*=\s*[\"']"
)

# web/static/js 树：已全部归零，无基线条目——任何新增直接违规。
STATIC_JS_BASELINE: dict[str, int] = {}

# 嵌入式 UI 页面基线（下一批迁移目标，只减不增）。
EMBEDDED_BASELINE: dict[str, int] = {
    "features/redmine/ui/page.js": 62,
    "features/redmine/ui/page.html": 53,
    "features/automation/ui/page.html": 47,
    "features/gerrit/ui/page.html": 39,
    "features/automation/ui/page.js": 16,
    "features/system/update_monitor/ui/page.html": 11,
    "features/system/mainline_issues/ui/page.html": 3,
    "features/cluster/ui/page.js": 2,
    "features/cluster/ui/page.html": 1,
}

# shell.html：已全部迁移归零（act-bridge 事件委托），无基线条目。
SHELL_BASELINE: dict[str, int] = {}

_ALL_BASELINES = {**STATIC_JS_BASELINE, **EMBEDDED_BASELINE, **SHELL_BASELINE}


def _scan(paths, baseline: dict[str, int]) -> list[str]:
    offenders = []
    for path, relative in paths:
        count = len(
            INLINE_HANDLER_RE.findall(
                path.read_text(encoding="utf-8", errors="ignore")
            )
        )
        limit = baseline.get(relative, 0)
        if count > limit:
            offenders.append(
                f"{relative}: {count} inline handlers (baseline {limit})"
            )
    return offenders


class InlineHandlerRatchetTests(unittest.TestCase):
    def test_static_js_inline_handlers_within_baseline(self):
        js_dir = ROOT / "web/static/js"
        paths = [
            (path, str(path.relative_to(ROOT)))
            for path in sorted(js_dir.rglob("*.js"))
            if "vendor" not in path.parts
        ]
        self.assertEqual(_scan(paths, STATIC_JS_BASELINE), [])

    def test_embedded_ui_inline_handlers_within_baseline(self):
        paths = [
            (ROOT / relative, relative)
            for relative in EMBEDDED_BASELINE
        ]
        self.assertEqual(_scan(paths, EMBEDDED_BASELINE), [])

    def test_shell_html_inline_handlers_within_baseline(self):
        paths = [(ROOT / "web/shell/shell.html", "web/shell/shell.html")]
        self.assertEqual(_scan(paths, SHELL_BASELINE), [])

    def test_baseline_entries_still_exist(self):
        """基线里的文件必须存在，防止例外条目腐化。"""

        missing = [
            relative
            for relative in _ALL_BASELINES
            if not (ROOT / relative).is_file()
        ]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
