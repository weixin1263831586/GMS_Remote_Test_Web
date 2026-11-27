"""Architecture typography and navigation in the actual browser renderer."""

import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness, expect


VIEWS = ('platform', 'usbip', 'aoa', 'firmware', 'test-flow', 'agent-analysis')


class RuntimeArchitectureViewsTests(RuntimeUiHarness):
    def test_six_visual_scenes_have_clear_nodes_and_keep_shared_tabs(self):
        page = self.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        expected = {'platform': 7, 'usbip': 7, 'aoa': 7, 'firmware': 8,
                    'test-flow': 9, 'agent-analysis': 11}
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('architecture', null)")
            page.wait_for_function(
                "document.querySelector('#architecture-iframe').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
            frame = page.frame_locator('#architecture-iframe')
            child = page.locator('#architecture-iframe').element_handle().content_frame()
            output = os.getenv("GMS_UI_PREVIEW_DIR")
            if output:
                directory = Path(output) / "architecture"
                directory.mkdir(parents=True, exist_ok=True)
                for name in VIEWS:
                    frame.locator(f'[data-arch="{name}"]').click()
                    expect(frame.locator(f'[data-arch-pane="{name}"]')).to_have_attribute(
                        'data-arch-scene-ready', 'true')
                    page.screenshot(path=str(directory / f'{name}.png'), animations='disabled')
            for name in VIEWS:
                frame.locator(f'[data-arch="{name}"]').click()
                pane = frame.locator(f'[data-arch-pane="{name}"]')
                expect(pane).to_be_visible()
                expect(pane).to_have_attribute('data-arch-scene-ready', 'true')
                expect(pane.locator('.arch-scene-node')).to_have_count(expected[name])
                expect(pane.locator('.arch-scene-sidecard')).to_have_count(2)
                expect(pane.locator('.arch-scene-wires')).to_be_visible()
                # One visible SVG stroke per edge; no duplicate blurred halo.
                self.assertTrue(pane.locator('.arch-scene-wires').evaluate('''svg => {
                    const paths = [...svg.querySelectorAll('.arch-scene-path')];
                    return paths.length > 0 &&
                        svg.querySelectorAll('.arch-scene-path-halo').length === 0 &&
                        paths.every(path => getComputedStyle(path).filter === 'none') &&
                        svg.querySelectorAll('.arch-scene-edge-pill').length === paths.length;
                }'''), name)
                expect(pane.locator('.arch-scene-ground')).to_have_count(0)
                if name == 'test-flow':
                    self.assertTrue(pane.locator('.arch-scene-canvas').evaluate('''canvas => {
                        const logs = canvas.querySelector('[data-scene-node="logs"]');
                        const report = canvas.querySelector('[data-scene-node="report"]');
                        return logs && report && logs.offsetLeft === report.offsetLeft &&
                            logs.offsetWidth === report.offsetWidth;
                    }'''), name)
                # All non-straight routes use rounded Q elbows and avoid
                # passing through the interior of nodes (endpoints excluded).
                self.assertTrue(pane.locator('.arch-scene-wires').evaluate('''svg => {
                    const canvas = svg.parentNode;
                    const boxes = [...canvas.querySelectorAll('.arch-scene-node')].map(n => {
                        const x = parseFloat(n.style.left), y = parseFloat(n.style.top);
                        const w = parseFloat(n.style.width), h = parseFloat(n.style.height);
                        return { left:x+3, right:x+w-3, top:y+3, bottom:y+h-3 };
                    });
                    const paths = [...svg.querySelectorAll('.arch-scene-path')];
                    // Straight-only routes are intentional; verify segments do
                    // not intersect nodes without mandating decorative curves.
                    if (!paths.length) return false;
                    return paths.every(path => {
                        const length = path.getTotalLength();
                        for (let distance=0; distance<=length; distance+=6) {
                            const point=path.getPointAtLength(distance);
                            if (boxes.some(b => point.x>b.left && point.x<b.right
                                && point.y>b.top && point.y<b.bottom)) return false;
                        }
                        return true;
                    });
                }'''), name)
                expect(pane.locator('.arch-scene-scroll')).to_be_visible()
                # Compact chips must stay single-line, preserving the complete
                # value in the title even on narrow Source and Tradefed cards.
                self.assertTrue(pane.locator('.arch-scene-canvas').evaluate('''canvas => {
                    return [...canvas.querySelectorAll('.arch-scene-chip')].every(chip => {
                        const label = chip.querySelector('span');
                        return !!label && getComputedStyle(label).whiteSpace === 'nowrap'
                            && !!chip.title && label.getBoundingClientRect().height < 23;
                    });
                }'''), name)
                if name == 'test-flow':
                    self.assertTrue(pane.locator('.arch-scene-canvas').evaluate('''canvas => {
                        const node = id => canvas.querySelector('[data-scene-node="'+id+'"]');
                        const center = n => n.offsetLeft + n.offsetWidth/2;
                        return ['plan','repair','redmine'].every(id =>
                            Math.abs(center(node(id)) - center(node('plan'))) < 1);
                    }'''), name)
                # Reject extremely short rendered arrow shafts; the labels
                # and arrowheads must have enough clear space to remain legible.
                self.assertTrue(pane.locator('.arch-scene-wires').evaluate('''svg => {
                    const paths = [...svg.querySelectorAll('.arch-scene-path')];
                    return paths.length > 0 && paths.every(path =>
                        path.getTotalLength() >= 43);
                }'''), name)

                self.assertTrue(pane.locator('.arch-scene-canvas').evaluate(
                    "canvas => canvas.style.transform === 'none' || canvas.style.transform.startsWith('scaleX(')"
                ), name)
                expect(pane.locator('.arch-v2-heading')).to_have_count(0)
                expect(pane.locator('.arch-v2-intro')).to_have_count(0)
                self.assertFalse(child.locator('.lead').count(), name)
                self.assertTrue(pane.locator('.arch-scene-canvas').evaluate(
                    "canvas => Number.parseFloat(getComputedStyle(canvas).zoom) > 0"
                ), name)
                expect(pane.locator('.arch-details')).to_have_count(0)
                self.assertTrue(pane.locator('.arch-scene-canvas').evaluate('''canvas => {
                    const box = canvas.getBoundingClientRect();
                    const nodes = [...canvas.querySelectorAll('.arch-scene-node')];
                    return nodes.every(node => {
                        const n = node.getBoundingClientRect();
                        return n.left >= box.left && n.top >= box.top
                            && n.right <= box.right && n.bottom <= box.bottom;
                    });
                }'''), name)
                # Text/chips must fit inside their own node, and the final row
                # must retain a visible margin above the bottom edge.
                self.assertTrue(pane.locator('.arch-scene-canvas').evaluate('''canvas => {
                    const bounds = canvas.getBoundingClientRect();
                    return [...canvas.querySelectorAll('.arch-scene-node')].every(node => {
                        const box = node.getBoundingClientRect();
                        return node.scrollHeight <= node.clientHeight + 3
                            && node.scrollWidth <= node.clientWidth + 3
                            && box.bottom <= bounds.bottom - 10;
                    });
                }'''), name)
                self.assertTrue(pane.locator('.arch-scene-wires').evaluate('''svg => {
                    const nodes = [...svg.parentNode.querySelectorAll('.arch-scene-node')];
                    return [...svg.querySelectorAll('.arch-scene-edge-pill')].every(pill => {
                        const a = pill.getBoundingClientRect();
                        return nodes.every(node => {
                            const b = node.getBoundingClientRect();
                            return a.right <= b.left || a.left >= b.right
                                || a.bottom <= b.top || a.top >= b.bottom;
                        });
                    });
                }'''), name)
                if name != 'platform':
                    expect(pane.locator('.arch-v2-mini')).to_have_count(3)
                self.assertTrue(pane.locator('.arch-scene-scroll').evaluate(
                    'el => el.scrollWidth >= el.clientWidth'), name)

            frame.locator('[data-arch="agent-analysis"]').focus()
            page.keyboard.press('ArrowRight')
            expect(frame.locator('[data-arch="platform"]')).to_have_attribute('aria-selected', 'true')
            page.keyboard.press('ArrowLeft')
            expect(frame.locator('[data-arch="agent-analysis"]')).to_have_attribute('aria-selected', 'true')
            self.assertIn('arch=agent-analysis', child.url)
            child.goto(child.url, wait_until='domcontentloaded')
            expect(frame.locator('[data-arch="agent-analysis"]')).to_have_attribute('aria-selected', 'true')
            expect(frame.locator('[data-arch-pane="agent-analysis"] .arch-scene-node')).to_have_count(11)

            page.set_viewport_size({'width': 600, 'height': 900})
            for name in VIEWS:
                frame.locator(f'[data-arch="{name}"]').click()
                pane = frame.locator(f'[data-arch-pane="{name}"]')
                self.assertTrue(child.evaluate(
                    '() => document.documentElement.scrollWidth <= window.innerWidth'), name)
                self.assertTrue(pane.locator('.arch-scene-scroll').evaluate(
                    'el => el.scrollWidth > el.clientWidth'), name)
            self.assert_no_page_errors(errors)
        finally:
            page.close()
