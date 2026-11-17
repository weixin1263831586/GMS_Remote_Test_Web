"""Keep route connectivity dialog headers aligned with the shared Wi-Fi glyph.

This is a source-level guard; browser screenshots remain the visual authority.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_route_dialogs_reuse_wifi_icon():
    wifi = (ROOT / "web/shell/dialogs/wifi.html").read_text(encoding="utf-8")
    vpn = (ROOT / "web/static/js/pages/vpn-control.js").read_text(encoding="utf-8")
    gerrit = (ROOT / "features/gerrit/ui/page.html").read_text(encoding="utf-8")
    icon = '/static/icons/ui.svg#wifi'
    assert icon in wifi
    route = vpn[vpn.index("async function checkRouting("):]
    assert icon in route
    assert 'class="tech-title-glyph"' in route
    assert '<h3 class="modal-title">检查路由连通性</h3>' in gerrit
    assert '<h3>📡 检查路由连通性</h3>' not in gerrit
    assert icon in gerrit[gerrit.index('<div id="routeCheckModal"'):]


def test_no_duplicate_vpn_route_header_wifi_path():
    vpn = (ROOT / "web/static/js/pages/vpn-control.js").read_text(encoding="utf-8")
    route = vpn[vpn.index("async function checkRouting("):]
    header = route.split('<div class="modal-body route-check-form">', 1)[0]
    assert '<use href="/static/icons/ui.svg#wifi"></use>' in header
    assert 'M4.6 10.2a10 10 0 0 1 14.8 0' not in header
