"""Architecture typography and navigation in the actual browser renderer."""

from tests.runtime_ui.harness import RuntimeUiHarness, expect


VIEWS = ('platform', 'usbip', 'aoa', 'firmware', 'test-flow', 'agent-analysis')


class RuntimeArchitectureViewsTests(RuntimeUiHarness):
    def test_diagrams_keep_readable_type_after_repeated_navigation(self):
        page = self.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            self.goto_shell(page)
            frame = page.frame_locator('#architecture-iframe')
            for _ in range(2):
                page.evaluate("switchPage('architecture', null)")
                scale = None
                for name in VIEWS:
                    frame.locator(f'[data-arch="{name}"]').click()
                    pane = frame.locator(f'[data-arch-pane="{name}"]')
                    expect(pane).to_be_visible()
                    for section in pane.locator('.arch-details section').all():
                        expect(section).to_be_visible()
                    metrics = pane.locator('.topology svg').evaluate('''svg => {
                        const sizes = selector => [...svg.querySelectorAll(selector)].map(text => {
                            const matrix = text.getScreenCTM();
                            return parseFloat(getComputedStyle(text).fontSize) * Math.hypot(matrix.a, matrix.b);
                        });
                        const overflow = [...svg.querySelectorAll('g[data-node], g[data-source]')].filter(group => {
                            if (!group.querySelector('rect')) return false;
                            const box = group.querySelector('rect').getBBox();
                            const text = group.querySelector('text').getBBox();
                            return text.x < box.x || text.y < box.y
                                || text.x + text.width > box.x + box.width
                                || text.y + text.height > box.y + box.height;
                        });
                        const diagram = svg.getBoundingClientRect();
                        const legend = svg.parentElement.querySelector('.arch-legend').getBoundingClientRect();
                        const firstNodeTop = Math.min(...[...svg.querySelectorAll('[data-node] rect')]
                            .map(rect => rect.getBoundingClientRect().top));
                        return {nodes: sizes('[data-node] text'),
                            edges: sizes('[data-source] text'), overflow: overflow.length,
                            scale: svg.getScreenCTM().a,
                            overlappingLegend: diagram.right > legend.left,
                            legendTopOffset: legend.top - firstNodeTop};
                    }''')
                    self.assertEqual(metrics['overflow'], 0, name)
                    if scale is None:
                        scale = metrics['scale']
                    self.assertAlmostEqual(metrics['scale'], scale, delta=0.001, msg=name)
                    self.assertFalse(metrics['overlappingLegend'], name)
                    self.assertAlmostEqual(metrics['legendTopOffset'], 0, delta=0.1, msg=name)
                    for size in metrics['nodes']:
                        self.assertAlmostEqual(size, 14 * scale, delta=0.1)
                        self.assertGreaterEqual(size, 11.7)
                    for size in metrics['edges']:
                        self.assertAlmostEqual(size, 12 * scale, delta=0.1)
                        self.assertGreaterEqual(size, 10)
                page.evaluate("switchPage('test', null)")

            # Both added flows participate in keyboard navigation and restored selection.
            page.evaluate("switchPage('architecture', null)")
            frame.locator('[data-arch="agent-analysis"]').focus()
            page.keyboard.press('ArrowRight')
            expect(frame.locator('[data-arch="platform"]')).to_have_attribute('aria-selected', 'true')
            page.keyboard.press('ArrowLeft')
            expect(frame.locator('[data-arch="agent-analysis"]')).to_have_attribute('aria-selected', 'true')
            child = page.locator('#architecture-iframe').element_handle().content_frame()
            self.assertIn('arch=agent-analysis', child.url)
            child.goto(child.url, wait_until='domcontentloaded')
            expect(frame.locator('[data-arch="agent-analysis"]')).to_have_attribute('aria-selected', 'true')
            details = frame.locator('[data-arch-pane="agent-analysis"] .arch-details')
            expect(details.locator('#agent-flow-details')).to_be_visible()
            details.locator('summary').click()
            expect(details.locator('#agent-flow-details')).to_be_hidden()
            details.locator('summary').click()
            expect(details.locator('#agent-flow-details')).to_be_visible()

            page.set_viewport_size({'width': 600, 'height': 900})
            for name in VIEWS:
                frame.locator(f'[data-arch="{name}"]').click()
                self.assertTrue(child.evaluate('''() =>
                    document.documentElement.scrollWidth <= window.innerWidth
                '''), name)
                self.assertTrue(frame.locator(f'[data-arch-pane="{name}"] .topology-viewport').evaluate(
                    'box => box.scrollWidth > box.clientWidth'
                ), name)
            self.assert_no_page_errors(errors)
        finally:
            page.close()
