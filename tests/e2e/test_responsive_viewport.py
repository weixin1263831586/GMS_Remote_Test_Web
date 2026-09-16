"""Responsive viewport matrix E2E.

For every page and a matrix of viewports (mobile portrait through desktop),
assert the document never overflows horizontally:

    document.documentElement.scrollWidth
        <= document.documentElement.clientWidth + tolerance

Table wrappers are allowed to scroll horizontally *inside* their own
container — the rule only rejects page-level overflow. Embedded pages
(devices-console / cluster / redmine-agent / gerrit-dashboard) render
inside iframes, so their INTERNAL document geometry is checked with the
same rule across a dedicated iframe viewport matrix — a clean parent
shell does not prove the iframe content fits.

Modal reachability (header visible, footer/close button tappable) is
checked for the device config modal at the narrowest viewport, using the
stable ``data-testid="device-config-open"`` contract: a missing key
control is a FAILURE, not a skip.

Skipped automatically when Playwright is unavailable (same contract as
``test_all_controls``).
"""
from __future__ import annotations

from tests import test_runtime_ui_smoke as runtime_ui_smoke
from tests.e2e.test_all_controls import ALL_PAGES


# Minimum viewport matrix — phone portrait, tablet, laptop,
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

# Embedded documents: activating the shell page lazy-loads the iframe, then
# the same overflow rule is evaluated on the frame's own document.
IFRAME_PAGES = [
    ("devices-console", "#devices-console-frame"),
    ("cluster", "#cluster-frame"),
    ("redmine-agent", "#redmine-agent-frame"),
    ("gerrit-dashboard", "#gerrit-dashboard-frame"),
]
IFRAME_VIEWPORT_WIDTHS = (360, 390, 768, 1024, 1440)
IFRAME_VIEWPORTS = [
    viewport for viewport in VIEWPORTS
    if viewport["width"] in IFRAME_VIEWPORT_WIDTHS
]

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

    def test_embedded_iframes_do_not_overflow_horizontally(self):
        """A clean parent shell proves nothing about iframe-internal layout.

        devices-console / cluster / redmine-agent / gerrit-dashboard render
        inside iframes hosted by the shell; activate each page through the
        application's own navigation entrypoint (this lazy-loads the iframe
        src) and run the same overflow rule on the frame's own document
        across the mobile/tablet/desktop subset of the viewport matrix.
        """
        page = self.new_page()
        self.addCleanup(page.close)
        for viewport in IFRAME_VIEWPORTS:
            page.set_viewport_size(viewport)
            page.goto(self.base_url, wait_until="domcontentloaded")
            page.wait_for_selector(".sidebar-item[data-page]")
            self.close_initial_modals(page)
            for page_name, frame_selector in IFRAME_PAGES:
                with self.subTest(viewport=viewport, frame=page_name):
                    page.evaluate("name => window.switchPage(name)", page_name)
                    page.wait_for_function(
                        "sel => Boolean(document.querySelector(sel)?.getAttribute('src'))",
                        arg=frame_selector,
                        timeout=15000,
                    )
                    frame = self.frame_for(page, frame_selector)
                    frame.wait_for_function(
                        "document.readyState === 'complete'"
                    )
                    result = frame.evaluate(OVERFLOW_SCRIPT, _OVERFLOW_TOLERANCE_PX)
                    self.assertLessEqual(
                        result["overflow"],
                        _OVERFLOW_TOLERANCE_PX,
                        (
                            f"horizontal overflow {result['overflow']}px inside "
                            f"iframe {page_name} ({frame_selector}) at "
                            f"{viewport['width']}x{viewport['height']}; "
                            f"offenders: {result['offenders']}"
                        ),
                    )

    def test_device_config_modal_controls_reachable_on_narrow_viewport(self):
        """The dcfg-toolbar must wrap instead of clipping inputs.

        Stable contract: the trigger is ``data-testid="device-config-open"``
        (never a guessed onclick/text selector), and a missing trigger or
        modal is a FAILURE — silently skipping would hide a broken entry
        point behind a green build.
        """
        page = self.new_page()
        self.addCleanup(page.close)
        page.set_viewport_size(VIEWPORTS[0])  # 360x800 — narrowest
        # Routes must be registered BEFORE navigation so the first device
        # inventory request (/api/devices/management) is served by the mock.
        page.route(
            "**/api/devices/management*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=(
                    '{"devices":[{"serial_no":"CFG-E2E-1",'
                    '"status":"online","source_host":"e2e-local"}]}'
                ),
            ),
        )
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.wait_for_selector(".sidebar-item[data-page]")
        self.close_initial_modals(page)
        page.evaluate("() => window.switchPage('devices')")
        page.wait_for_function(
            "Boolean(document.querySelector('[data-testid=\"device-config-open\"]'))",
            timeout=15000,
        )
        # 客户端身份识别弹框（/api/users/detect 失败时）是异步晚到的，
        # 可能落在初始 2s 窗口之后（满负载运行时尤其明显）；点击目标
        # 前再清一次，避免弹层拦截指针事件造成假失败。
        self.close_initial_modals(page)
        page.locator('[data-testid="device-config-open"]').first.click()

        modal = page.locator("#device-config-modal")
        self.assertEqual(
            modal.count(), 1, "device config modal missing after trigger click"
        )
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
                '#device-config-modal .modal-close,'
                + ' #device-config-modal [aria-label="关闭"],'
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
