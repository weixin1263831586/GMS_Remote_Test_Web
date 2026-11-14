"""Layout gate for the tools/websites card grid.

常用工具/常用网址 卡片网格曾由三条渲染路径各自写死内联
``grid-template-columns``（utility-tools.js 本地路径 + server-sync.js
同步路径×2），内联样式优先级高于样式表，导致只改 CSS 的列数调整
反复不生效。现统一由 common-layout.css 的 ``.tools-grid`` 类控制。

门禁目的：
* 网格列数/间距只有 .tools-grid 一处事实来源；
* 任何 JS/模板不得再对 tools-grid 元素内联 grid 样式。
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"

_CSS = WEB / "static" / "css" / "common-layout.css"

# 匹配对 tools-grid 元素施加内联 grid 样式的两种历史写法：
# 1) innerHTML/cssText: <div class="tools-grid" style="...grid-template-columns...">
# 2) cards.style.cssText = 'display: grid; grid-template-columns: ...;'
_INLINE_GRID_ON_TOOLS_GRID = re.compile(
    r'tools-grid["\'][^>]*style="[^"]*grid-template-columns'
    r'|(?:style\.cssText\s*=\s*[`"\'][^`"\']*)grid-template-columns(?=[^`"\']*[;`"\'])',
    re.IGNORECASE,
)
_JS_OR_HTML_SUFFIXES = {".js", ".html"}


def test_tools_grid_columns_have_single_source_of_truth():
    css = _CSS.read_text(encoding="utf-8")
    match = re.search(
        r"\.tools-grid\s*\{[^}]*grid-template-columns:\s*([^;}]+)", css, re.DOTALL
    )
    assert match, "common-layout.css 必须定义 .tools-grid 的 grid-template-columns"
    # 桌面端固定 12 列（历史 auto-fill 写法曾受系统缩放/窗口宽度影响，
    # 无法稳定达到预期列数）。
    assert match.group(1).strip() == "repeat(12, 1fr)", (
        f".tools-grid 应固定 12 列，实际是: {match.group(1).strip()}"
    )


def test_no_inline_grid_styles_on_tools_grid_elements():
    offenders: list[str] = []
    for base in (WEB / "static" / "js", WEB / "shell"):
        for path in base.rglob("*"):
            if path.suffix.lower() not in _JS_OR_HTML_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if _INLINE_GRID_ON_TOOLS_GRID.search(text):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "tools-grid 禁止内联 grid 样式（列数只由 .tools-grid 类控制）: "
        + ", ".join(offenders)
    )
