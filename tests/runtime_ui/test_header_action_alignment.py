"""One title-action geometry template for segmented navigation headers.

Desktop/Terminal's four layout buttons and Users/Audit's tab capsules must
start at exactly the same top edge and use the same 26px outer control height.
"""

import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness


class HeaderActionAlignmentTests(RuntimeUiHarness):
    def test_segmented_action_groups_share_top_and_height(self):
        page = self.new_page()
        evidence = {}
        try:
            self.goto_shell(page)
            for width, height in ((1912, 914), (1440, 900), (960, 720),
                                  (680, 800), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                geometry = page.evaluate(
                    """() => {
                        const names = ['terminal', 'users', 'desktop', 'security-audit'];
                        const pages = [...document.querySelectorAll(
                            '.main-container > .page-content')];
                        const oldActive = pages.map(node => node.classList.contains('active'));
                        const oldBodyClass = document.body.className;
                        const samples = {};
                        try {
                            document.body.classList.remove(
                                'workspace-scope-single', 'workspace-scope-pending');
                            document.body.classList.add('workspace-scope-cluster');
                            for (const name of names) {
                                const root = document.getElementById('page-' + name);
                                const oldReady = root.classList.contains('host-workspace-ready');
                                const actions = root.querySelector('.page-header-actions');
                                const group = root.querySelector(
                                    '.host-workspace-layouts, .users-view-tabs');
                                if (!actions || !group) throw Error(
                                    'Missing header action template for ' + name);
                                const oldDisplay = group.style.display;
                                pages.forEach(node => node.classList.toggle(
                                    'active', node === root));
                                root.classList.add('host-workspace-ready');
                                group.style.display = 'flex';
                                try {
                                    const rootBox = root.getBoundingClientRect();
                                    const actionBox = actions.getBoundingClientRect();
                                    const groupBox = group.getBoundingClientRect();
                                    const buttons = [...group.querySelectorAll('button')]
                                        .map(node => node.getBoundingClientRect().height)
                                        .filter(height => height > 0);
                                    samples[name] = {
                                        top: groupBox.top - rootBox.top,
                                        right: rootBox.right - groupBox.right,
                                        actionTop: actionBox.top - rootBox.top,
                                        height: groupBox.height,
                                        buttons, count: buttons.length,
                                        whiteSpace: getComputedStyle(group).whiteSpace,
                                        visible: getComputedStyle(group).display !== 'none',
                                    };
                                } finally {
                                    group.style.display = oldDisplay;
                                    root.classList.toggle('host-workspace-ready', oldReady);
                                }
                            }
                        } finally {
                            pages.forEach((node, i) => node.classList.toggle(
                                'active', oldActive[i]));
                            document.body.className = oldBodyClass;
                        }
                        return samples;
                    }"""
                )
                evidence[str(width)] = geometry
                self.assertEqual(len(geometry), 4)
                tops = [item["top"] for item in geometry.values()]
                heights = [item["height"] for item in geometry.values()]
                for name, item in geometry.items():
                    with self.subTest(width=width, page=name):
                        self.assertTrue(item["visible"], item)
                        self.assertGreaterEqual(item["count"], 1, item)
                        self.assertAlmostEqual(item["top"], 8, delta=0.75, msg=str(item))
                        self.assertAlmostEqual(item["right"], 8, delta=0.75, msg=str(item))
                        self.assertAlmostEqual(item["actionTop"], 8,
                                               delta=0.75, msg=str(item))
                        self.assertAlmostEqual(item["height"], 26,
                                               delta=0.75, msg=str(item))
                        self.assertLessEqual(max(item["buttons"]), 26.5, item)
                        expectedButton = 24 if name in ("users", "security-audit") else 26
                        for buttonHeight in item["buttons"]:
                            self.assertAlmostEqual(
                                buttonHeight, expectedButton, delta=0.75,
                                msg=str(item),
                            )
                self.assertLessEqual(max(tops) - min(tops), 0.75, geometry)
                self.assertLessEqual(max(heights) - min(heights), 0.75, geometry)
            target = os.getenv("GMS_UI_PREVIEW_DIR")
            if target:
                directory = Path(target)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "header-action-alignment.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        finally:
            page.close()
