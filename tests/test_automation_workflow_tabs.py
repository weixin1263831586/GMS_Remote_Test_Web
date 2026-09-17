from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "features/automation/ui/page.html"
KEYBOARD = ROOT / "web/static/js/automation-workflow-tabs.js"


def test_ats_tablist_contains_only_workflow_tabs_not_toolbar_actions():
    html = PAGE.read_text(encoding="utf-8")
    tablist_start = html.index('<div class="workflow-tabs" role="tablist"')
    actions_start = html.index('<div class="ats-actions"', tablist_start)
    # The tablist is explicitly closed before Worker/Refresh/More controls.
    between = html[tablist_start:actions_start]
    assert between.rstrip().endswith("</div>")
    assert "worker-indicator" not in between
    assert "automation-toast" not in between
    assert '<div class="ats-toolbar-main">' in html


def test_ats_workflow_tabs_support_roving_keyboard_navigation():
    source = KEYBOARD.read_text(encoding="utf-8")
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in source
    assert "tab.tabIndex = tab === selected ? 0 : -1;" in source
    assert "next.focus();" in source
    assert "next.click();" in source


def test_ats_page_loads_keyboard_controller():
    html = PAGE.read_text(encoding="utf-8")
    assert "/static/js/automation-workflow-tabs.js" in html
