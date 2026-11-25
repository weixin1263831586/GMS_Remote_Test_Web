"""Static guardrails for the purple tech modal skin across all UI documents.

This suite protects the existing modal lifecycle. Browser focus/stack/viewport
regressions remain covered by tests/runtime_ui/test_modals.py.
"""
import re
from itertools import pairwise
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TECH_STYLE = ROOT / "web/static/css/modal-tech.css"
SHELL_HEAD = ROOT / "web/shell/components/head.html"
EMBEDDED_UI = (
    "features/redmine/ui/page.html",
    "features/gerrit/ui/page.html",
    "features/automation/ui/page.html",
    "features/cluster/ui/page.html",
    "features/devices/ui/page.html",
    "features/gms/radar/ui/page.html",
)


def test_skin_is_solid_and_does_not_own_modal_lifecycle():
    css = TECH_STYLE.read_text(encoding="utf-8")
    assert "var(--primary-color" in css
    assert "background-color: rgb(18 18 24 / 40%)" in css
    assert "backdrop-filter: blur(3px) saturate(100%)" in css
    assert "blur(14px)" not in css
    assert "rgb(9 8 15 / 72%)" not in css
    assert "linear-gradient(" not in css
    assert "radial-gradient(" not in css
    assert "conic-gradient(" not in css
    assert "display: none" not in css
    assert "display: flex" not in css
    assert "z-index: var(--modal-z-index" not in css
    assert ".modal.show" not in css
    assert ".modal-backdrop[hidden]" not in css
    assert ".modal-content.modal-pin" not in css
    assert ".modal-content.modal-workspace" not in css
    assert "#route-check-dialog" in css
    assert "#config-modal" in css
    assert "#radar-settings-save" in css


def test_sdk_source_registration_uses_content_height_not_pinned_empty_space():
    style = (ROOT / "web/static/css/agent-access.css").read_text(encoding="utf-8")
    source = (ROOT / "web/shell/dialogs/agent-enrollment.html").read_text(
        encoding="utf-8"
    )
    assert "#agent-sdk-add-modal > .agent-access-modal-content" in style
    assert "height: auto;" in style
    assert "max-height: min(86dvh" in style
    assert "overflow-y: auto;" in style
    assert 'id="agent-sdk-add-form"' in source
    assert 'id="agent-sdk-add-submit"' in source


def test_shell_style_bundle_keeps_modal_skin_inside_contiguous_sequence():
    source = SHELL_HEAD.read_text(encoding="utf-8")
    links = list(re.finditer(
        r'<link rel="stylesheet" href="(/static/css/[^"]+)" data-shell-styles>',
        source,
    ))
    assert len(links) > 8
    names = [match.group(1) for match in links]
    assert sum("/modal-tech.css?" in name for name in names) == 1
    assert names.index(next(name for name in names if "/modal-tech.css?" in name)) > (
        names.index(next(name for name in names if "/modal.css?" in name))
    )
    assert all(not source[left.end():right.start()].strip()
               for left, right in pairwise(links))


def test_embedded_documents_load_the_same_skin_after_modal_base():
    for path in EMBEDDED_UI:
        source = (ROOT / path).read_text(encoding="utf-8")
        assert source.count('href="/static/css/modal-tech.css?') == 1, path
        assert source.index('href="/static/css/modal.css?') < source.index(
            'href="/static/css/modal-tech.css?'
        ), path
    radar = (ROOT / "features/gms/radar/ui/page.html").read_text(
        encoding="utf-8"
    )
    assert radar.index("update-radar-settings.css?") < radar.index(
        "modal-tech.css?"
    )


def test_tailscale_visual_resize_preserves_original_behaviors():
    source = (ROOT / "web/shell/dialogs/tailscale-info.html").read_text(
        encoding="utf-8"
    )
    assert 'id="tailscale-info-modal"' in source
    assert 'class="modal-content modal-l tech-dialog tech-dialog--tailscale"' in source
    for hook in (
        'id="tailscale-url-display"',
        'data-click="copyTailscaleAccessUrl"',
        'data-click="copyTailscaleInstallCmd"',
        'data-click="closeTailscaleInfoModal"',
        'https://tailscale.com/download/windows',
        'https://tailscale.com/download/mac',
        'https://tailscale.com/download/android',
    ):
        assert hook in source
