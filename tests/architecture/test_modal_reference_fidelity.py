"""Guard the approved Purple Tech modal's structural layout contract."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ssh_actions_are_not_nested_in_scrollable_body():
    markup = (ROOT / "web/shell/dialogs/username-detect.html").read_text(encoding="utf-8")
    assert 'id="username-detect-form"' in markup
    body_end = markup.index('            </div>\n            <div class="modal-buttons tech-dialog-actions">')
    assert body_end > markup.index('class="modal-body tech-ssh-composition"')
    assert markup.count('data-click="submitUsernameDetect"') == 1
    assert markup.count('data-click="closeUsernameDetectModal"') >= 1


def test_reference_fidelity_rules_are_scoped_and_responsive():
    css = (ROOT / "web/static/css/modal-tech-compositions.css").read_text(encoding="utf-8")
    scoped = css.split("/* Reference fidelity pass (2026-11-17).", 1)[1]
    assert "#username-detect-modal > .modal-content > .tech-dialog-actions" in scoped
    assert "font-size: 18px !important" in scoped
    assert "@media (max-width: 670px)" in scoped
    assert "pointer-events: none" not in scoped
