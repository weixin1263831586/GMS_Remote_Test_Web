"""Regression contracts for business-specific modal compositions.

These checks reject "one generic card for all dialogs" while preserving
each modal's existing IDs, event dispatch and responsive CSS contracts.
Visual accuracy still requires real Chromium screenshots on the deployed app.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "web/shell/dialogs"
COMPOSITIONS = ROOT / "web/static/css/modal-tech-compositions.css"


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.actions: list[tuple[str, str | None]] = []
        self.classes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if values.get("class"):
            self.classes.extend(values["class"].split())
        if values.get("data-click"):
            self.actions.append((values["data-click"], values.get("data-a0")))


def parse(name: str) -> Tags:
    parser = Tags()
    parser.feed((SHELL / (name + ".html")).read_text(encoding="utf-8"))
    return parser


def test_distinct_sync_strategy_actions_keep_original_arguments() -> None:
    parsed = parse("mainline-sync")
    assert "tech-mainline-choice-grid" in parsed.classes
    assert "tech-mainline-option--incremental" in parsed.classes
    assert "tech-mainline-option--full" in parsed.classes
    assert parsed.actions.count(("ut_confirmMainlineKnownIssuesSync", "incremental")) == 1
    assert parsed.actions.count(("ut_confirmMainlineKnownIssuesSync", "full")) == 1
    assert parsed.actions.count(("ut_closeMainlineSyncModal", None)) == 2
    assert len(parsed.ids) == len(set(parsed.ids))


def test_archive_extraction_separates_source_and_target_without_losing_fields() -> None:
    parsed = parse("extract-suite")
    assert "tech-extract-source" in parsed.classes
    assert "tech-extract-target" in parsed.classes
    for node_id in (
        "extract-suite-archive-select",
        "extract-suite-archive-path",
        "extract-suite-folder-name",
        "btn-submit-extract-suite",
    ):
        assert parsed.ids.count(node_id) == 1
    assert ("submitExtractSuite", None) in parsed.actions
    assert ("closeExtractSuiteModal", None) in parsed.actions
    assert len(parsed.ids) == len(set(parsed.ids))


def test_gsi_required_and_optional_images_preserve_original_file_pickers() -> None:
    parsed = parse("gsi")
    for css_class in ("tech-gsi-required", "tech-gsi-optional"):
        assert css_class in parsed.classes
    for node_id in ("gsi-script", "gsi-system", "gsi-vendor"):
        assert parsed.ids.count(node_id) == 1
    for action in (
        "browseLocalFileForGsiScript",
        "browseLocalFileForGsiSystem",
        "pickLocalGsiSystemFile",
        "browseLocalFileForGsiVendor",
        "browseRemoteFileForGsiVendor",
        "submitGsiBurn",
    ):
        assert any(name == action for name, _ in parsed.actions), action
    assert len(parsed.ids) == len(set(parsed.ids))


def test_wifi_content_preserves_validation_and_only_original_actions() -> None:
    parsed = parse("wifi")
    assert "tech-wifi-hint" in parsed.classes
    assert "tech-purpose-panel" in parsed.classes
    assert parsed.ids.count("wifi-ssid") == 1
    assert parsed.ids.count("wifi-password") == 1
    assert parsed.ids.count("wifi-form") == 1
    assert ("closeWifiModal", None) in parsed.actions
    assert len(parsed.ids) == len(set(parsed.ids))


def test_specialized_compositions_have_mobile_fallbacks() -> None:
    source = COMPOSITIONS.read_text(encoding="utf-8")
    for selector in (
        "#mainline-sync-modal .tech-mainline-choice-grid",
        "#extract-suite-modal .tech-extract-source",
        "#extract-suite-modal .tech-extract-target",
        "#gsi-modal .tech-gsi-required",
        "#gsi-modal .tech-gsi-optional",
        "#wifi-modal .tech-wifi-hint",
    ):
        assert selector in source, selector
    assert "grid-template-columns:1fr" in source
    assert "linear-gradient(" not in source
