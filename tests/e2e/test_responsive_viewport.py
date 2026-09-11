"""Responsive viewport matrix E2E (12.txt P1).

For every page and a matrix of viewports (mobile portrait through desktop),
assert the document never overflows horizontally:

    document.documentElement.scrollWidth
        <= document.documentElement.clientWidth + tolerance

Table wrappers are allowed to scroll horizontally *inside* their own
container — the rule only rejects page-level overflow. Modal reachability
(header visible, footer/close button tappable) is checked for the device
config modal at the narrowest viewport.

Skipped automatically when Playwright is unavailable (same contract as
``test_all_controls``).
"""
from __future__ import annotations

from tests import test_runtime_ui_smoke as runtime_ui_smoke
from tests.e2e.test_all_controls import ALL_PAGES


# 12.txt §八: minimum viewport matrix — phone portrait, tablet, laptop,
# desktop. Tolerance absorbs scrollbar rounding differences across browsers.
VIEWPORTS = [
    {"width": 360, "height": 800},
    {"width": 390, "height": 844},
    {"width": 768, "height": 1024},
    {"width": 1024, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]
_OVERFLOW_TOLERANCE_PX = 2

OVERFLOW_SCRIPT = """
(tolerance) => {
  const doc = document.documentElement;
  const overflow = doc.scrollWidth - doc.clientWidth;
  const offenders = [];
  if (overflow > tolerance) {
    // Locate the widest elements that stick out past the client edge to
    // make CI failures actionable.
    const edge = doc.clientWidth;
    for (const element of document.querySelectorAll('body *')) {
      const rect = element.getBoundingClientRect();
      if (rect.width > 0 && rect.right > edge + tolerance) {
        offenders.push(
          (element.id ? '#' + element.id : element.tagName.toLowerCase())
          + ' right=' + Math.round(rect.right)
        );
        if (offenders.length >= 5) break;
      }
    }
  }
  return {overflow, clientWidth: doc.clientWidth, offenders};
}
"""


class ResponsiveViewportE2ETests(runtime_ui_smoke.RuntimeUiHarness):
    maxDiff = None

    def test_pages_do_not_overflow_horizontally_across_viewports(self):
        page = self.new_page()
        self.addCleanup(page.close)
        for viewport in VIEWPORTS:
            page.set_viewport_size(viewport)
            for page_name in ALL_PAGES:
                with self.subTest(viewport=viewport, page=page_name):
                    page.goto(f"{self.base_url}/{page_name}", wait_until="domcontentloaded")
                    result = page.evaluate(OVERFLOW_SCRIPT, _OVERFLOW_TOLERANCE_PX)
                    self.assertLessEqual(
                        result["overflow"],
                        _OVERFLOW_TOLERANCE_PX,
                        (
                            f"horizontal overflow {result['overflow']}px on "
                            f"/{page_name} at {viewport['width']}x{viewport['height']}; "
                            f"offenders: {result['offenders']}"
                        ),
                    )

    def test_device_config_modal_controls_reachable_on_narrow_viewport(self):
        """The dcfg-toolbar must wrap instead of clipping inputs (12.txt §八)."""
        page = self.new_page()
        self.addCleanup(page.close)
        page.set_viewport_size(VIEWPORTS[0])  # 360x800 — narrowest
        page.goto(f"{self.base_url}/devices", wait_until="domcontentloaded")

        open_device_config = page.evaluate(
            """
            () => {
              const trigger = document.querySelector(
                '[onclick*="openDeviceConfig"], [onclick*="DeviceConfig"], #device-config-btn'
              );
              if (!trigger) return {found: false};
              trigger.click();
              return {found: true};
            }
            """
        )
        if not open_device_config.get("found"):
            self.skipTest("device config modal trigger not present on devices page")

        modal = page.locator("#device-config-modal")
        if modal.count() == 0:
            self.skipTest("device config modal not present")
        modal.wait_for(state="visible", timeout=5000)

        result = page.evaluate(
            """
            () => {
              const clipped = [];
              const docEdge = document.documentElement.clientWidth;
              for (const element of document.querySelectorAll(
                '#device-config-modal .dcfg-toolbar input,'
                + '#device-config-modal .dcfg-toolbar select,'
                + '#device-config-modal .dcfg-toolbar button'
              )) {
                const style = getComputedStyle(element);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const rect = element.getBoundingClientRect();
                if (rect.right > docEdge + 1 || rect.left < -1) {
                  clipped.push(element.id || element.className || element.tagName);
                }
              }
              const closeBtn = document.querySelector(
                '#device-config-modal .close,'
                + ' #device-config-modal [onclick*="close"],'
                + ' #device-config-modal [aria-label="Close"]'
              );
              const closeVisible = Boolean(
                closeBtn
                && closeBtn.getBoundingClientRect().width > 0
                && closeBtn.getBoundingClientRect().right <= docEdge + 1
              );
              return {clipped, closeVisible};
            }
            """
        )
        self.assertEqual(
            result["clipped"],
            [],
            f"toolbar controls clipped outside viewport: {result['clipped']}",
        )
        self.assertTrue(result["closeVisible"], "modal close button not reachable")


if __name__ == "__main__":
    import unittest

    unittest.main()
