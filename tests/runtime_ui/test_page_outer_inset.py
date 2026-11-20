"""Every shell page uses the same 8px outer inset at every viewport size.

This checks the actual loaded CSS in Chromium, including the canonical
page-content padding and the shared (title-excluding) body frame. Internal
card, table, iframe and modal spacing are intentionally outside this contract.
"""

import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness


class UnifiedPageOuterInsetTests(RuntimeUiHarness):
    def test_all_shell_pages_share_8px_perimeter_at_all_breakpoints(self):
        page = self.new_page()
        matrix = {}
        try:
            self.goto_shell(page)
            for width, height in ((1792, 914), (1440, 900), (960, 720),
                                  (680, 800), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                measured = page.evaluate(
                    """() => {
                        const pages = [...document.querySelectorAll(
                            '.main-container > .page-content')];
                        const original = pages.map(page =>
                            page.classList.contains('active'));
                        const token = getComputedStyle(document.documentElement)
                            .getPropertyValue('--ui-page-edge').trim();
                        const samples = [];
                        try {
                            for (const root of pages) {
                                pages.forEach(node =>
                                    node.classList.toggle('active', node === root));
                                const style = getComputedStyle(root);
                                const rect = root.getBoundingClientRect();
                                const header = root.querySelector(
                                    ':scope > .page-header-row');
                                const title = header?.getBoundingClientRect();
                                const frame = getComputedStyle(root, '::after');
                                samples.push({
                                    id: root.id,
                                    display: style.display,
                                    padding: [
                                        style.paddingTop, style.paddingRight,
                                        style.paddingBottom, style.paddingLeft,
                                    ],
                                    hasHeader: Boolean(header),
                                    frame: {
                                        top: frame.top,
                                        right: frame.right,
                                        bottom: frame.bottom,
                                        left: frame.left,
                                        display: frame.display,
                                    },
                                    titleOffset: title && (title.left - rect.left),
                                    frameBorderY: rect.top + parseFloat(frame.top),
                                    titleBottom: title?.bottom,
                                    width: rect.width,
                                });
                            }
                        } finally {
                            pages.forEach((root, index) =>
                                root.classList.toggle('active', original[index]));
                        }
                        return {token, samples};
                    }"""
                )
                matrix[str(width)] = measured
                self.assertEqual(measured["token"], "8px", width)
                self.assertGreaterEqual(len(measured["samples"]), 20, width)
                ids = {item["id"] for item in measured["samples"]}
                self.assertTrue(
                    {"page-test", "page-users", "page-desktop", "page-terminal",
                     "page-devices", "page-security-audit", "page-report-analysis",
                     "page-apk-analysis", "page-test-suites", "page-cluster"}.issubset(ids),
                    f"Missing shell pages at {width}px: {ids}",
                )
                for item in measured["samples"]:
                    with self.subTest(viewport=width, page=item["id"]):
                        self.assertEqual(item["display"], "flex", item)
                        self.assertEqual(item["padding"], ["8px"] * 4, item)
                        if item["hasHeader"]:
                            self.assertAlmostEqual(
                                item["titleOffset"], 8, delta=0.75,
                                msg=str(item),
                            )
                        if item["id"] != "page-test":
                            self.assertEqual(
                                [item["frame"][edge]
                                 for edge in ("right", "bottom", "left")],
                                ["8px"] * 3,
                                item,
                            )
                            # Body-frame top is derived from title geometry.
                            # The header can wrap on mobile; independent route
                            # geometry tests cover that after page activation.
                            self.assertGreaterEqual(
                                item["frameBorderY"], 8, item,
                            )
            target = os.getenv("GMS_UI_PREVIEW_DIR")
            if target:
                output = Path(target)
                output.mkdir(parents=True, exist_ok=True)
                (output / "unified-page-edge-8px.json").write_text(
                    json.dumps(matrix, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        finally:
            page.close()
