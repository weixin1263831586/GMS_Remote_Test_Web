"""Structural contracts for purpose-built purple console dialog compositions.

These checks intentionally verify real DOM structure and original action hooks;
a passing CSS snapshot alone is not considered dialog refactoring.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIALOGS = ROOT / "web/shell/dialogs"
EMBEDDED_VARIANTS = {
    "features/redmine/ui/page.html": (
        "redmine-reset", "redmine-user", "redmine-department",
        "redmine-project", "redmine-settings", "redmine-trend-range",
        "redmine-trend-detail", "redmine-batch-import",
        "redmine-knowledge", "redmine-reference",
        "redmine-internal-create",
    ),
    "features/gerrit/ui/page.html": (
        "gerrit-settings", "gerrit-personal", "gerrit-department",
        "gerrit-owner", "gerrit-trend-range", "gerrit-trend-detail",
        "gerrit-route",
    ),
    "features/cluster/ui/page.html": (
        "cluster-onboarding", "cluster-worker-config",
    ),
    "features/devices/ui/page.html": ("serial-binding",),
}


class DialogTagValidator(HTMLParser):
    """Conservative explicit-tag structure check; HTML void tags are skipped."""

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        pass

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        expected = self.stack.pop() if self.stack else None
        if tag != expected:
            self.errors.append(f"expected </{expected}> got </{tag}>")


def test_shell_templates_are_balanced_and_individually_identified() -> None:
    templates = sorted(DIALOGS.glob("*.html"))
    assert len(templates) == 40
    for path in templates:
        html = path.read_text(encoding="utf-8")
        expected = "tailscale" if path.stem == "tailscale-info" else path.stem
        assert f"tech-dialog--{expected}" in html, path.name
        assert "tech-corner-mark--tl" in html, path.name
        assert "tech-corner-mark--br" in html, path.name
        parser = DialogTagValidator()
        parser.feed(html)
        parser.close()
        assert not parser.errors, (path.name, parser.errors[:6])
        assert not parser.stack, (path.name, parser.stack[-8:])


def test_multi_modal_agent_templates_have_independent_sections() -> None:
    source = (DIALOGS / "agent-enrollment.html").read_text(encoding="utf-8")
    for variant in ("agent-enrollment", "sdk-source", "agent-scopes"):
        assert f"tech-dialog--{variant}" in source
    for content in ("tech-enrollment-layout", "tech-sdk-source-layout"):
        assert content in source
    for hook in (
        'id="agent-access-enroll-form"',
        'id="agent-sdk-add-form"',
        'id="agent-access-token-scope-list"',
    ):
        assert hook in source


def test_business_panels_have_real_markup_not_only_global_css() -> None:
    required = {
        "firmware.html": ("tech-firmware-layout", "tech-firmware-source-panel",
                          "tech-firmware-recovery-panel",
                          'data-click="submitFirmwareBurn"'),
        "report-copy.html": ("tech-report-transfer-grid", "tech-transfer-source",
                             "tech-transfer-target",
                             'id="report-copy-target-suite"'),
        "report-diagnosis.html": ("tech-diagnosis-layout",
                                 "tech-diagnosis-summary-pane",
                                 "tech-diagnosis-result-pane",
                                 'id="report-diagnostic-result"'),
        "username-detect.html": ("tech-ssh-composition", "tech-ssh-host-row",
                                "tech-ssh-user-row", "tech-ssh-auth-row",
                                'id="username-detect-password"'),
        "device-group.html": ("tech-group-members-panel",
                             'id="device-group-picklist"'),
        "kb-upload.html": ("tech-knowledge-dropzone",
                           'id="kb-upload-input"'),
        "adb-proxy.html": ("tech-routing-setup-panel",
                           "tech-routing-current",
                           'id="adb-proxy-source-devices"'),
        "usbip-attach.html": ("tech-routing-setup-panel",
                             "tech-routing-current",
                             'id="usbip-source-device"'),
        "firmware-share.html": ("tech-share-setup", "tech-share-inventory",
                               'id="firmware-share-list"'),
        "wifi.html": ("tech-purpose-panel", 'id="wifi-ssid"', 'id="wifi-password"'),
        "gsi.html": ("tech-image-sources", 'id="gsi-system"'),
        "tool.html": ("tech-tool-details", 'id="tool-url"'),
        "ut-tool.html": ("tech-tool-details", 'id="ut-tool-filepath"'),
        "weekly-report.html": ("tech-weekly-output", 'id="weekly-report-content"'),
    }
    for name, markers in required.items():
        source = (DIALOGS / name).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in source, (name, marker)


def test_embedded_workspace_dialogs_have_individual_content_compositions() -> None:
    for filename, variants in EMBEDDED_VARIANTS.items():
        source = (ROOT / filename).read_text(encoding="utf-8")
        for variant in variants:
            assert f"tech-dialog--{variant}" in source, (filename, variant)
            if variant.startswith(("redmine-", "gerrit-")):
                assert f"tech-embedded-workflow--{variant}" in source
            elif variant.startswith("cluster-"):
                assert "tech-cluster-intro" in source
            else:
                assert "tech-serial-binding-composition" in source


def test_styles_are_loaded_after_baseline_on_all_workspaces() -> None:
    pages = (
        "web/shell/components/head.html",
        *EMBEDDED_VARIANTS,
        "features/automation/ui/page.html",
        "features/system/update_radar/ui/page.html",
    )
    for filename in pages:
        source = (ROOT / filename).read_text(encoding="utf-8")
        common = source.index("/static/css/modal-tech.css?")
        per_case = source.index("/static/css/modal-tech-cases.css?")
        composition = source.index("/static/css/modal-tech-compositions.css?")
        assert common < per_case < composition, filename
        assert source.count("/static/css/modal-tech-compositions.css?") == 1


def test_workflow_css_keeps_background_visible_and_never_clips_modal_root() -> None:
    layer = (ROOT / "web/static/css/modal-tech-compositions.css").read_text(
        encoding="utf-8"
    )
    backdrop = (ROOT / "web/static/css/modal-tech.css").read_text(
        encoding="utf-8"
    )
    assert "rgb(18 13 30 / 40%)" in backdrop
    assert "blur(3px)" in backdrop
    for gradient in ("linear-gradient(", "radial-gradient(", "conic-gradient("):
        assert gradient not in layer
    assert "clip-path: none !important" in layer
    assert "border: 1px solid #9a75bb !important" in layer
    for layout in (
        ".tech-ssh-form", ".tech-firmware-layout", ".tech-report-transfer-grid",
        ".tech-diagnosis-layout", ".tech-device-routing-composition",
        ".tech-tools-composition", ".tech-serial-binding-composition",
    ):
        assert layout in layer
