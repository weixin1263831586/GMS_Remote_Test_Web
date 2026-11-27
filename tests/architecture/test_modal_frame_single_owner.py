"""Guard against overlapping and misaligned Purple Tech modal outlines.

Reference style: one continuous SVG perimeter, with its corner accents on
the same path, and a title rail/highlight sharing the same SVG baseline.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIALOGS = ROOT / "web/shell/dialogs"
CSS = ROOT / "web/static/css/modal-tech-dialogs.css"
FRAME_JS = ROOT / "web/static/js/dialog-frame.js"


def test_every_shell_dialog_uses_one_svg_perimeter_and_one_header_rule():
    templates = sorted(DIALOGS.glob("*.html"))
    assert len(templates) >= 38
    assert not (DIALOGS / 'config.html').exists()
    assert not (DIALOGS / 'gms-assistant-config.html').exists()
    for file in templates:
        markup = file.read_text(encoding="utf-8")
        shells = re.findall(r'<div[^>]+class="[^"]*\bmodal-content\b[^"]*\btech-dialog\b[^"]*"', markup)
        assert shells, file.name
        # Some templates (for example Agent enrollment) contain several dialogs.
        assert len(re.findall(r'class="tech-frame"', markup)) == len(shells), file.name
        assert len(re.findall(r'class="tech-header-rule"', markup)) == len(shells), file.name
        assert markup.count('class="modal-close"') >= len(shells), file.name


def test_no_second_rectangular_border_over_svg_cut_corners():
    css = CSS.read_text(encoding="utf-8")
    scoped = css.split("Single-line ownership for SVG-framed dialogs:", 1)[1]
    assert "clip-path: polygon(" in scoped
    assert "--tech-corner-cut: 16px;" in scoped
    assert ".modal-content.tech-dialog:has(> .tech-frame)" in scoped
    assert "border: 0 !important" in scoped
    assert ".modal-content.tech-dialog:not(:has(> .tech-frame))" in css
    assert "border: 1px solid #9867bd !important" in css


def test_header_svg_owns_its_rail_and_glowing_underline():
    css = CSS.read_text(encoding="utf-8")
    scoped = css.split("Single-line ownership for SVG-framed dialogs:", 1)[1]
    assert ":has(> .tech-header-rule)" in scoped
    assert "border-bottom: 0 !important" in scoped
    assert "content: none !important" in scoped
    assert ".tech-rule-highlight" in scoped
    js = FRAME_JS.read_text(encoding="utf-8")
    assert "path(svg, 'tech-rule-highlight')" in js
    assert "M18 ${baseline}" in js
    assert "M0 ${baseline}H" in js
    assert "M2 28V16L16 2H28" in js


def test_pointer_events_and_semantics_are_preserved():
    css = CSS.read_text(encoding="utf-8")
    assert ".tech-dialog .tech-frame {" in css
    assert "pointer-events: none" in css
    js = FRAME_JS.read_text(encoding="utf-8")
    assert "ResizeObserver" in js
    assert "MutationObserver" in js
