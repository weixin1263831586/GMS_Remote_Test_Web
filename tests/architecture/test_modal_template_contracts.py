"""Source contracts for the entire shell dialog catalog."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIALOGS = ROOT / "web/shell/dialogs"


def test_all_shell_dialogs_share_frame_and_header_glyph():
    templates = sorted(DIALOGS.glob("*.html"))
    assert len(templates) >= 38
    assert not (DIALOGS / 'config.html').exists()
    assert not (DIALOGS / 'gms-assistant-config.html').exists()
    for file in templates:
        markup = file.read_text(encoding="utf-8")
        count = len(re.findall(r'class="[^"]*\bmodal-content\b[^"]*\btech-dialog\b', markup))
        assert count > 0, file.name
        assert markup.count('class="tech-frame"') == count, file.name
        assert markup.count('class="tech-header-rule"') == count, file.name
        assert markup.count('class="tech-title-glyph"') == count, file.name


def test_all_icon_symbols_exist():
    symbols = set(re.findall(r'<symbol\s+id="([^"]+)"', (ROOT / "web/static/icons/ui.svg").read_text(encoding="utf-8")))
    assert "wifi" in symbols
    for file in sorted(DIALOGS.glob("*.html")):
        used = set(re.findall(r'<use\s+href="/static/icons/ui\.svg#([^"]+)"', file.read_text(encoding="utf-8")))
        assert used <= symbols, (file.name, used - symbols)


def test_dynamic_route_and_inspection_do_not_retain_duplicate_chrome():
    control = (DIALOGS / "ui-control.html").read_text(encoding="utf-8")
    route = (ROOT / "web/static/js/pages/vpn-control.js").read_text(encoding="utf-8")
    route = route[route.index("async function checkRouting("):]
    assert 'class="ui-control-workspace"' in control
    assert 'min-height: 400px' not in control
    assert '🎯' not in control
    assert '</span></div></span>' not in control
    assert route.count('class="tech-header-rule"') == 1
    assert 'class="route-check-corner' not in route
    assert 'class="route-check-rule"' not in route
    assert 'class="route-check-ticks"' not in route
    assert 'ui.svg#wifi' in route


def test_radar_footer_is_outside_form_scroll_area_and_actions_are_right_aligned():
    markup = (DIALOGS / "update-radar-settings.html").read_text(encoding="utf-8")
    radar_css = (ROOT / "web/static/css/update-radar-settings.css").read_text(encoding="utf-8")
    cases_css = (ROOT / "web/static/css/modal-tech-cases.css").read_text(encoding="utf-8")
    common_css = (ROOT / "web/static/css/modal-tech-dialogs.css").read_text(encoding="utf-8")
    assert 'form="radar-settings-form"' in markup
    assert markup.index('</form>') < markup.index('class="modal-buttons tech-dialog-actions radar-settings-actions"')
    assert 'height:430px' not in radar_css
    assert 'position:sticky' not in radar_css
    assert 'justify-content: flex-start; margin-top: 0;' not in cases_css
    assert 'justify-content: flex-end;' in common_css


def test_other_long_dialogs_keep_footer_outside_scroll_content():
    for name in ("sidebar-visibility.html", "wifi.html", "file-browser.html"):
        source = (DIALOGS / name).read_text(encoding="utf-8")
        assert 'class="modal-buttons tech-dialog-actions"' in source or 'class="modal-buttons tech-dialog-actions sidebar-settings-footer"' in source
    sidebar = (DIALOGS / "sidebar-visibility.html").read_text(encoding="utf-8")
    assert sidebar.index('id="sidebar-settings-panel-guide"') < sidebar.index('class="modal-buttons tech-dialog-actions sidebar-settings-footer"')
    file_browser = (DIALOGS / "file-browser.html").read_text(encoding="utf-8")
    assert 'justify-content: space-between;' not in file_browser
    assert file_browser.index('class="file-browser-nav-actions"') < file_browser.index('class="modal-buttons tech-dialog-actions"')
    wifi = (DIALOGS / "wifi.html").read_text(encoding="utf-8")
    assert 'form="wifi-form"' in wifi
    assert wifi.index('</form>') < wifi.index('class="modal-buttons tech-dialog-actions"')
