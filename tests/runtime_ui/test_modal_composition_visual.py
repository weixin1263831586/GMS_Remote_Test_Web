"""Capture actual modal renderings and reject geometry or contrast regressions.

This gallery is a review artifact, never a fabricated marketing mockup. It
uses RuntimeUiHarness' authenticated isolated app server and Chromium.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness

ROOT = Path(__file__).resolve().parents[2]
REVIEW_ROOT = ROOT / "artifacts/modal-review"
# These dialogs intentionally exercise different workflow shapes and sizes.
SAMPLES = (
    "tailscale-info-modal",
    "username-detect-modal",
    "firmware-modal",
    "gsi-modal",
    "report-copy-modal",
    "report-diagnosis-modal",
    "device-group-modal",
    "wifi-modal",
    "kb-upload-modal",
    "agent-sdk-add-modal",
    "agent-access-create-modal",
    "adb-proxy-modal",
    "usbip-attach-modal",
    "tool-modal",
    "config-modal",
)
VIEWPORTS = ((1440, 900), (390, 844))


class ModalCompositionVisualTests(RuntimeUiHarness):
    def test_real_dialog_gallery_and_computed_style_contracts(self) -> None:
        REVIEW_ROOT.mkdir(parents=True, exist_ok=True)
        page = self.new_page()
        screenshots: list[dict[str, str | int]] = []
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            self.goto_shell(page)
            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                for modal_id in SAMPLES:
                    if page.locator(f"#{modal_id}").count() != 1:
                        self.fail(f"Missing independent modal: {modal_id}")
                    page.evaluate("(id) => ModalManager.open(id)", modal_id)
                    panel = page.locator(f"#{modal_id} .modal-content")
                    panel.wait_for(state="visible")
                    computed = panel.evaluate(
                        """panel => {
                            const r = panel.getBoundingClientRect();
                            const css = getComputedStyle(panel);
                            const overlay = getComputedStyle(panel.closest('.modal'));
                            return {
                                left:r.left, right:r.right, top:r.top, bottom:r.bottom,
                                border:css.borderTopWidth, clip:css.clipPath,
                                backdrop:overlay.backgroundColor,
                            };
                        }"""
                    )
                    try:
                        with self.subTest(modal=modal_id, viewport=width):
                            self.assertGreaterEqual(computed["left"], -1)
                            self.assertLessEqual(computed["right"], width + 1)
                            self.assertGreaterEqual(computed["top"], -1)
                            self.assertLessEqual(computed["bottom"], height + 1)
                            self.assertEqual(computed["border"], "1px")
                            self.assertEqual(computed["clip"], "none")
                            self.assertEqual(
                                computed["backdrop"], "rgba(18, 13, 30, 0.4)"
                            )
                            path = REVIEW_ROOT / f"{modal_id}-{width}x{height}.png"
                            page.screenshot(
                                path=str(path), animations="disabled",
                                full_page=False, timeout=12000,
                            )
                            screenshots.append({
                                "name": modal_id, "width": width,
                                "height": height, "file": path.name,
                            })
                    finally:
                        page.evaluate("(id) => ModalManager.close(id)", modal_id)
            (REVIEW_ROOT / "manifest.json").write_text(
                json.dumps(screenshots, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.assertFalse(page_errors, page_errors[:8])
        finally:
            page.close()
