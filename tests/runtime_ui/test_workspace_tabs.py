"""The six embedded workspaces share one tab geometry at every viewport size."""

from tests.runtime_ui.harness import RuntimeUiHarness, expect


WORKSPACES = (
    ('devices-console', 'devices-console-frame'),
    ('architecture', 'architecture-iframe'),
    ('automation', 'automation-frame'),
    ('cluster', 'cluster-frame'),
    ('redmine-agent', 'redmine-agent-frame'),
    ('gerrit-dashboard', 'gerrit-dashboard-frame'),
)

GEOMETRY = """list => {
    list.scrollLeft = 0;
    const box = node => {
        const r = node.getBoundingClientRect();
        return {x: r.x, y: r.y, width: r.width, height: r.height};
    };
    return {
        list: box(list), header: box(list.parentElement),
        tabs: [...list.querySelectorAll('[role="tab"]')].map(tab => {
            const style = getComputedStyle(tab);
            const line = getComputedStyle(tab, '::after');
            return {...box(tab), font: style.font, color: style.color,
                background: style.backgroundColor,
                line: tab.classList.contains('active')
                    ? {height: line.height, bottom: line.bottom, color: line.backgroundColor} : null};
        }),
        overflow: document.documentElement.scrollWidth - innerWidth,
    };
}"""


class RuntimeWorkspaceTabsTests(RuntimeUiHarness):
    def test_six_tab_bars_share_geometry_after_resize_and_navigation(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            reference_font = None
            reference_active = None
            for width, height in ((1440, 960), (1920, 1080), (900, 700), (390, 844)):
                page.set_viewport_size({'width': width, 'height': height})
                frame_origin = None
                for name, frame_id in WORKSPACES:
                    with self.subTest(page=name, width=width):
                        page.evaluate('name => switchPage(name, null)', name)
                        tablist = page.frame_locator('#' + frame_id).locator('.workspace-tabs')
                        expect(tablist).to_be_visible()
                        self.frame_for(page, '#' + frame_id).wait_for_load_state('domcontentloaded')
                        expect(page.locator('#' + frame_id)).to_be_visible(timeout=15000)
                        origin = page.locator('#' + frame_id).bounding_box()
                        frame_origin = frame_origin or origin
                        for axis in ('x', 'y'):
                            self.assertAlmostEqual(origin[axis], frame_origin[axis], delta=0.5)
                        geometry = tablist.evaluate(GEOMETRY)
                        self.assertAlmostEqual(geometry['header']['y'], 0, delta=0.5)
                        self.assertAlmostEqual(geometry['list']['x'], 8, delta=0.5)
                        self.assertAlmostEqual(geometry['list']['y'], 0, delta=0.5)
                        self.assertAlmostEqual(geometry['list']['height'], 44, delta=0.5)
                        self.assertLessEqual(geometry['overflow'], 1)
                        if width >= 1440:
                            last = geometry['tabs'][-1]
                            self.assertLessEqual(
                                last['x'] + last['width'],
                                geometry['list']['x'] + geometry['list']['width'] + 1,
                                'desktop actions must not clip the last tab',
                            )
                        for index, tab in enumerate(geometry['tabs']):
                            self.assertAlmostEqual(tab['width'], 112, delta=0.5)
                            self.assertAlmostEqual(tab['height'], 44, delta=0.5)
                            self.assertAlmostEqual(tab['x'], 8 + index * 112, delta=0.5)
                            self.assertAlmostEqual(tab['y'], 0, delta=0.5)
                            reference_font = reference_font or tab['font']
                            self.assertEqual(tab['font'], reference_font)
                            if tab['line']:
                                active = (tab['color'], tab['background'], tab['line'])
                                reference_active = reference_active or active
                                self.assertEqual(active, reference_active)
                        # Re-entry must retain the same row geometry without reloading.
                        page.evaluate("switchPage('test', null)")
                        page.evaluate('name => switchPage(name, null)', name)
                        self.assertEqual(tablist.evaluate(GEOMETRY), geometry)
                        if width == 390:
                            tabs = tablist.get_by_role('tab')
                            tabs.first.focus()
                            for _ in range(tabs.count() - 1):
                                page.keyboard.press('ArrowRight')
                            expect(tabs.last).to_have_attribute('aria-selected', 'true')
                            self.assertTrue(tabs.last.evaluate("""tab => {
                                const rect = tab.getBoundingClientRect();
                                const row = tab.parentElement.getBoundingClientRect();
                                return rect.left >= row.left - 1 && rect.right <= row.right + 1;
                            }"""), 'keyboard navigation must reveal the complete active tab')
        finally:
            page.close()

    def test_ats_menu_opens_outside_the_scrollable_tab_row(self):
        page = self.new_page()
        try:
            page.goto(f'{self.base_url}/automation', wait_until='domcontentloaded')
            for width in (1280, 390):
                page.set_viewport_size({'width': width, 'height': 844})
                page.locator('.ats-more-btn').click()
                item = page.get_by_role('button', name='轮询 Gerrit')
                expect(item).to_be_visible()
                self.assertTrue(item.evaluate("""node => {
                    const rect = node.getBoundingClientRect();
                    return node.contains(document.elementFromPoint(
                        rect.x + rect.width / 2, rect.y + rect.height / 2));
                }"""))
                self.assertEqual(page.locator('.workspace-tabs .workspace-actions').count(), 0)
                page.locator('.ats-more-btn').click()
        finally:
            page.close()
