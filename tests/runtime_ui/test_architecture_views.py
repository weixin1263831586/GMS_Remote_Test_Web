"""Architecture typography and navigation in the actual browser renderer."""

from tests.runtime_ui.harness import RuntimeUiHarness, expect


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
                for name in ('platform', 'usbip', 'aoa', 'firmware'):
                    frame.locator(f'[data-arch="{name}"]').click()
                    pane = frame.locator(f'[data-arch-pane="{name}"]')
                    expect(pane).to_be_visible()
                    metrics = pane.locator('.topology svg').evaluate('''svg => {
                        const sizes = selector => [...svg.querySelectorAll(selector)].map(text => {
                            const matrix = text.getScreenCTM();
                            return parseFloat(getComputedStyle(text).fontSize) * Math.hypot(matrix.a, matrix.b);
                        });
                        const overflow = [...svg.querySelectorAll('g[data-node]')].filter(group => {
                            const box = group.querySelector('rect').getBBox();
                            const text = group.querySelector('text').getBBox();
                            return text.x < box.x || text.y < box.y
                                || text.x + text.width > box.x + box.width
                                || text.y + text.height > box.y + box.height;
                        });
                        return {nodes: sizes('[data-node] text'),
                            edges: sizes('[data-source] text'), overflow: overflow.length};
                    }''')
                    self.assertEqual(metrics['overflow'], 0, name)
                    for size in metrics['nodes']:
                        self.assertAlmostEqual(size, 16, delta=0.1)
                    for size in metrics['edges']:
                        self.assertAlmostEqual(size, 12, delta=0.1)
                page.evaluate("switchPage('test', null)")
            page.set_viewport_size({'width': 600, 'height': 900})
            page.evaluate("switchPage('architecture', null)")
            child = page.locator('#architecture-iframe').element_handle().content_frame()
            self.assertTrue(child.evaluate('''() =>
                document.documentElement.scrollWidth <= window.innerWidth
            '''))
            self.assert_no_page_errors(errors)
        finally:
            page.close()
