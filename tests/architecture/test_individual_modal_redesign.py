"""Contracts for the individually rebuilt Purple-Tech dialog DOM and layout."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIALOG_ROOT = ROOT / "web/shell/dialogs"
SOURCE_FILES = (
    "web/static/css/modal-tech-dialogs.css",
    "web/static/css/modal-tech-cases.css",
)


def test_every_shell_dialog_has_its_own_structural_variant() -> None:
    dialog_files = sorted(DIALOG_ROOT.glob("*.html"))
    assert len(dialog_files) == 41
    for path in dialog_files:
        source = path.read_text(encoding="utf-8")
        assert "tech-dialog--" + ("tailscale" if path.stem == "tailscale-info" else path.stem) in source, path.name
        assert "tech-corner-mark--tl" in source, path.name
        assert "tech-corner-mark--br" in source, path.name
        if path.stem not in ("device-config",):
            assert "tech-title-group" in source, path.name
            assert "tech-title-glyph" in source, path.name
        assert "tech-dialog-header" in source, path.name


def test_standalone_dialogs_have_independent_variants() -> None:
    expected = {
        "features/redmine/ui/page.html": 11,
        "features/gerrit/ui/page.html": 7,
        "features/cluster/ui/page.html": 2,
        "features/devices/ui/page.html": 1,
    }
    for path, count in expected.items():
        source = (ROOT / path).read_text(encoding="utf-8")
        assert source.count("tech-dialog--") == count, path
        assert source.count("tech-corner-mark--tl") == count, path
        assert source.count("tech-corner-mark--br") == count, path


def test_tailscale_has_five_dedicated_icon_rows_with_original_callbacks() -> None:
    source = (DIALOG_ROOT / "tailscale-info.html").read_text(encoding="utf-8")
    assert source.count('class="modal-form-row tech-tailscale-row"') == 5
    assert source.count('class="tech-field-glyph"') == 5
    for hook in (
        'id="tailscale-url-display"',
        'data-click="copyTailscaleAccessUrl"',
        'data-click="copyTailscaleInstallCmd"',
        'data-click="_actOpenUrlBlank"',
        'data-click="closeTailscaleInfoModal"',
    ):
        assert hook in source
    styles = (ROOT / SOURCE_FILES[0]).read_text(encoding="utf-8")
    assert "width: min(820px" in styles
    assert "height: auto" in styles
    assert "tech-frame" in styles
    assert "Desktop sizing: compact" not in styles
    assert source.count('class="tech-brand-icon"') == 4
    assert "tech-footer-trace" in source
    assert "data-modal-initial-focus" in source
    assert ".tech-tailscale-row" in styles
    assert 'class="tech-frame"' in source


def test_modal_styles_contain_no_background_gradients_or_visibility_owner() -> None:
    for path in SOURCE_FILES:
        styles = (ROOT / path).read_text(encoding="utf-8")
        assert not re.search(r"(?:linear|radial|conic)-gradient\s*\(", styles)
        assert ".modal.show" not in styles
        assert ".modal-backdrop[hidden]" not in styles
        assert "z-index: var(--modal-z-index" not in styles
    head = (ROOT / "web/shell/components/head.html").read_text(encoding="utf-8")
    for name in ("modal-tech.css", "modal-tech-dialogs.css", "modal-tech-cases.css"):
        assert head.count("/static/css/" + name + "?") == 1


def test_per_dialog_css_is_not_only_a_shared_template() -> None:
    styles = (ROOT / SOURCE_FILES[1]).read_text(encoding="utf-8")
    for name in (
        "username-detect", "firmware", "report-copy", "device-group",
        "device-config", "report-diagnosis", "redmine-reset",
        "gerrit-owner", "cluster-onboarding", "serial-binding",
    ):
        assert ".tech-dialog--" + name in styles, name
    controller = (ROOT / "web/static/js/modal.js").read_text(encoding="utf-8")
    assert "tech-dialog--dynamic-analysis" in controller
