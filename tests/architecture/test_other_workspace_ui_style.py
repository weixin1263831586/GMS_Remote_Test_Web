"""Regression guards for visual refactor aligned with Agent / device inventory.

Static contract only: runtime UI tests still required before merging.
"""

import re
from itertools import pairwise
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "web" / "shell"
CSS = ROOT / "web" / "static" / "css"
PAGES = (
    "test", "desktop", "terminal", "users", "devices-console",
    "reports", "report-analysis", "apk-analysis", "test-suites", "api-docs",
    "architecture", "websites", "tools", "security-audit", "gms-assistant",
    "automation", "cluster", "gerrit-dashboard", "redmine-agent", "notes",
)


def test_only_other_pages_opt_in() -> None:
    for page in PAGES:
        source = (SHELL / "pages" / f"{page}.html").read_text(encoding="utf-8")
        assert 'class="container page-content page-ui-rework"' in source, page
    for page in ("agent", "devices"):
        source = (SHELL / "pages" / f"{page}.html").read_text(encoding="utf-8")
        assert "page-ui-rework" not in source, page


def test_shell_style_bundle_keeps_all_links_adjacent() -> None:
    source = (SHELL / "components" / "head.html").read_text(encoding="utf-8")
    pattern = re.compile(
        r'<link rel="stylesheet" href="(/static/css/[a-z0-9-]+\.css)'
        r'(?:\?[^"]*)?" data-shell-styles>'
    )
    matches = list(pattern.finditer(source))
    assert matches
    assert sum("other-workspaces.css" in m.group(1) for m in matches) == 1
    assert all(not source[a.end():b.start()].strip()
               for a, b in pairwise(matches))


def test_kpis_preserve_existing_metric_ids_and_actions() -> None:
    expected = {
        # users 页两个视图共用 page-stat-card 模板：用户列表 3 张 +
        # Agent 接入 3 张（agent-token-*-count），同页共 6 个图标。
        "users": ("total-users-count", "active-users-count", "testing-users-count",
                  "agent-token-total-count", "agent-token-active-count",
                  "agent-token-inactive-count"),
        "api-docs": ("total-apis-count", "get-apis-count", "post-apis-count",
                     "filtered-apis-count", "skills-count"),
        "security-audit": ("audit-total-count", "audit-web-count",
                           "audit-cli-count", "audit-error-count"),
    }
    for page, ids in expected.items():
        source = (SHELL / "pages" / f"{page}.html").read_text(encoding="utf-8")
        assert source.count('class="workbench-stat-icon"') == len(ids), page
        for field in ids:
            assert source.count(f'id="{field}"') == 1, (page, field)
    users = (SHELL / "pages" / "users.html").read_text(encoding="utf-8")
    # 统计卡为纯展示（与设备/审计页一致）：不允许点击筛选处理器；
    # 旧专属统计卡模板（agent-access-stat）与 intro 区已删除。
    assert 'setUsersStatusFilter' not in users
    assert 'data-user-status-card' not in users
    # 精确匹配旧专属统计卡模板的 class（不含 status-filter 下拉的子串）。
    assert 'class="agent-access-stat"' not in users
    assert 'agent-access-intro' not in users


def test_rework_styles_do_not_target_approved_workspaces() -> None:
    source = (CSS / "other-workspaces.css").read_text(encoding="utf-8")
    assert ".page-ui-rework" in source
    assert "#page-agent.page-ui-rework" not in source
    assert "#page-devices.page-ui-rework" not in source
    assert "@import" not in source
