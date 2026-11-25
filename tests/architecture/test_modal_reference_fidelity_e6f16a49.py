"""Dialog markup and business-hook contracts; visual fidelity requires Chromium."""
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIALOGS = ROOT / "web" / "shell" / "dialogs"
CSS = ROOT / "web" / "static" / "css"


class DialogStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
        self.ids = set()
        self.actions = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if values.get("data-click"):
            self.actions.append(values["data-click"])
        if tag in {"div", "section", "form"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in {"div", "section", "form"}:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append((tag, self.stack[-1] if self.stack else None))
        else:
            self.stack.pop()


def test_ssh_dialog_keeps_actions_and_has_no_redundant_section_title():
    markup = (DIALOGS / "username-detect.html").read_text(encoding="utf-8")
    parsed = DialogStructure()
    parsed.feed(markup)
    assert not parsed.errors and not parsed.stack
    assert {"username-detect-ip", "username-detect-username",
            "username-detect-password", "username-detect-form"} <= parsed.ids
    assert "submitUsernameDetect" in parsed.actions
    assert "closeUsernameDetectModal" in parsed.actions
    assert 'aria-label="SSH 连接信息"' in markup
    assert ">SSH 连接信息</h3>" not in markup


def test_diagnosis_keeps_data_binding_ids_and_new_two_panel_structure():
    markup = (DIALOGS / "report-diagnosis.html").read_text(encoding="utf-8")
    parsed = DialogStructure()
    parsed.feed(markup)
    assert not parsed.errors and not parsed.stack
    assert {"report-diagnostic-summary", "report-diagnostic-result"} <= parsed.ids
    assert {"rerunReportDiagnosis", "copyReportDiagnosis",
            "minimizeReportDiagnosisWorkbench", "closeReportDiagnosisWorkbench"} <= set(parsed.actions)
    assert 'aria-label="诊断摘要"' in markup
    assert 'aria-label="诊断分析结果"' in markup


def test_svg_framed_and_dynamic_dialogs_both_keep_borders():
    css = (CSS / "modal-tech-dialogs.css").read_text(encoding="utf-8")
    assert ".modal-content.tech-dialog:not(:has(> .tech-frame))" in css
    assert ".modal-content.tech-dialog:has(> .tech-frame)" in css
    assert "border: 1px solid #9867bd !important" in css
    assert "prefers-reduced-motion" in css
