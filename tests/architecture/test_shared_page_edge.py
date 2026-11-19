"""Shell page perimeter is owned by one canonical 12px geometry token."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "web" / "static" / "css"


def test_canonical_page_edge_token_has_no_breakpoint_duplicates():
    sizing = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
    common = (CSS / "common.css").read_text(encoding="utf-8")
    layout = (CSS / "common-layout.css").read_text(encoding="utf-8")
    page = (CSS / "common-page.css").read_text(encoding="utf-8")

    assert sizing.count("--ui-page-edge: 12px;") == 1
    assert "padding: var(--ui-page-edge);" in common
    assert "--page-body-frame-inset: var(--ui-page-edge);" in common
    assert "--page-body-frame-top: calc(" in common
    assert "right: var(--page-body-frame-inset);" in common
    assert "bottom: var(--page-body-frame-inset);" in common
    assert "left: var(--page-body-frame-inset);" in common

    # Prevent responsive 8px/10px page-level overrides from silently
    # reintroducing inconsistent outer spacing.
    for stylesheet in (common, layout, page):
        assert "--page-body-frame-inset: 8px" not in stylesheet
        assert "--page-body-frame-inset: 10px" not in stylesheet
        assert "--page-body-frame-inset: 15px" not in stylesheet
    assert ".container {\n        padding: 10px;" not in layout
    assert ".container {\n        padding: 8px;" not in page


def test_shell_pages_use_the_shared_page_content_template():
    pages = ROOT / "web" / "shell" / "pages"
    templates = sorted(pages.glob("*.html"))
    assert len(templates) >= 20
    for template in templates:
        markup = template.read_text(encoding="utf-8")
        assert "page-content" in markup, template.name
        assert "container" in markup, template.name
