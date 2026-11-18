"""Visual contracts for compact route and inspection dialogs."""
from tests.runtime_ui.harness import RuntimeUiHarness, expect


class ModalGlobalConsistencyTests(RuntimeUiHarness):
    def test_inspection_has_no_empty_400px_workspace(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("ModalManager.open('ui-control-modal')")
            modal = page.locator("#ui-control-modal")
            expect(modal.locator(".ui-control-workspace")).to_be_visible()
            for viewport in [{"width": 1440, "height": 960}, {"width": 390, "height": 844}]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    workspace = modal.locator(".ui-control-workspace").bounding_box()
                    screen = modal.locator(".ui-control-screen-panel").bounding_box()
                    items = modal.locator(".ui-control-elements-panel").bounding_box()
                    self.assertLess(workspace["height"], 400)
                    if viewport["width"] <= 800:
                        self.assertGreaterEqual(items["y"], screen["y"] + screen["height"] - 1)
                    else:
                        self.assertGreaterEqual(items["x"], screen["x"] + screen["width"] - 1)
        finally:
            page.close()

    def test_device_hierarchy_is_not_interpreted_as_html(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("ModalManager.open('ui-control-modal')")
            page.evaluate("""() => {
                renderUiControlElements([
                  {text:'<img src=x onerror=alert(1)>', class_name:'android.widget.TextView',
                    resource_id:'fixture-safe', center:[12,34], interactions:['clickable']},
                  {text:'fixture', class_name:'android.widget.TextView',
                    center:['1" onmouseover=alert(1)',44], interactions:['clickable']}
                ]);
            }""")
            rows = page.locator("#ui-control-elements .ui-ctrl-el")
            expect(rows).to_have_count(2)
            expect(rows.first).to_contain_text("<img src=x onerror=alert(1)>")
            self.assertEqual(page.locator("#ui-control-elements img").count(), 0)
            self.assertEqual(page.locator("#ui-control-elements [onmouseover]").count(), 0)
            self.assertEqual(page.locator("#ui-control-elements [role=button]").count(), 1)
            expect(rows.first.locator("svg use")).to_have_attribute(
                "href", "/static/icons/ui.svg#target"
            )
        finally:
            page.close()


    def test_firmware_share_grid_fits_desktop_and_scrolls_on_mobile(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("ModalManager.open('firmware-share-modal')")
            page.evaluate("""() => {
                const tbody = document.getElementById('firmware-share-list');
                tbody.replaceChildren();
                for (let i = 0; i < 26; i++) {
                    const row = document.createElement('tr');
                    [
                        'firmware-' + i + '-super-long-artifact-name-update.img',
                        'builder@example-host:/very/long/firmware/output/dir/'
                            + 'rk3588_a17-release-userdebug/' + 'component-'.repeat(12)
                            + '/firmware-' + i + '.img',
                        '2.4 GB', String(i * 3), '分享 / 下载 / 删除'
                    ].forEach((value, col) => {
                        const cell = document.createElement('td');
                        cell.textContent = value;
                        cell.className = [
                            'firmware-share-name', 'firmware-share-path',
                            'firmware-share-number', 'firmware-share-downloads',
                            'firmware-share-actions'
                        ][col];
                        row.append(cell);
                    });
                    tbody.append(row);
                }
            }""")
            modal = page.locator("#firmware-share-modal")
            table_area = modal.locator(".firmware-share-table-scroll")
            footer = modal.locator(".firmware-share-footer")
            for viewport in [
                {"width": 1440, "height": 900},
                {"width": 768, "height": 720},
                {"width": 390, "height": 844},
                {"width": 844, "height": 390},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    expect(table_area).to_be_visible()
                    expect(footer.get_by_text("创建分享")).to_be_visible()
                    metrics = table_area.evaluate("""el => ({
                      clientW: el.clientWidth, scrollW: el.scrollWidth,
                      clientH: el.clientHeight, scrollH: el.scrollHeight,
                      overflowX: getComputedStyle(el).overflowX,
                      overflowY: getComputedStyle(el).overflowY,
                    })""")
                    self.assertEqual(metrics["overflowX"], "auto")
                    self.assertEqual(metrics["overflowY"], "auto")
                    if viewport["width"] >= 1000:
                        # At desktop widths all five fields must fit without X-scrolling.
                        self.assertLessEqual(metrics["scrollW"], metrics["clientW"] + 2)
                        last_header = modal.locator(".firmware-share-col-actions").bounding_box()
                        panel = table_area.bounding_box()
                        self.assertLessEqual(last_header["x"] + last_header["width"], panel["x"] + panel["width"] + 1)
                    else:
                        self.assertGreater(metrics["scrollW"], metrics["clientW"] + 30)
                        table_area.evaluate("el => el.scrollLeft = el.scrollWidth")
                        self.assertGreater(table_area.evaluate("el => el.scrollLeft"), 0)
                    self.assertGreater(metrics["scrollH"], metrics["clientH"] + 20)
                    table_area.evaluate("el => el.scrollTop = el.scrollHeight")
                    self.assertGreater(table_area.evaluate("el => el.scrollTop"), 0)
                    bounds = modal.locator(".modal-content").bounding_box()
                    foot = footer.bounding_box()
                    self.assertLessEqual(foot["y"] + foot["height"], bounds["y"] + bounds["height"] + 1)
                    self.assertGreaterEqual(foot["y"], bounds["y"])
                    self.assertGreaterEqual(bounds["y"], -1)
                    self.assertLessEqual(bounds["y"] + bounds["height"], viewport["height"] + 1)
                    if viewport["width"] == 1440:
                        self.assertGreater(bounds["height"], 700)
        finally:
            page.close()

    def test_radar_footer_stays_visible_and_right_aligned_while_form_scrolls(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("ModalManager.open('radar-settings-modal')")
            modal = page.locator("#radar-settings-modal")
            body = modal.locator(":scope > .modal-content > .modal-body")
            footer = modal.locator(":scope > .modal-content > .radar-settings-actions")
            self.assertEqual(footer.count(), 1)
            self.assertEqual(modal.locator("#radar-settings-form .radar-settings-actions").count(), 0)
            expect(footer.locator("#radar-settings-cancel")).to_be_visible()
            for viewport in [
                {"width": 1440, "height": 900},
                {"width": 768, "height": 720},
                {"width": 390, "height": 844},
                {"width": 844, "height": 390},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    metrics = modal.evaluate("""el => {
                        const box = node => node.getBoundingClientRect();
                        const content = el.querySelector('.modal-content');
                        const body = content.querySelector(':scope > .modal-body');
                        const footer = content.querySelector(':scope > .radar-settings-actions');
                        const cancel = footer.querySelector('#radar-settings-cancel');
                        const save = footer.querySelector('#radar-settings-save');
                        return {
                            content: box(content).toJSON(), body: box(body).toJSON(),
                            footer: box(footer).toJSON(), cancel: box(cancel).toJSON(),
                            save: box(save).toJSON(), overflow: getComputedStyle(body).overflowY,
                            justify: getComputedStyle(footer).justifyContent,
                            scrollHeight: body.scrollHeight, clientHeight: body.clientHeight
                        };
                    }""")
                    self.assertEqual(metrics["justify"], "flex-end")
                    self.assertEqual(metrics["overflow"], "auto")
                    self.assertLessEqual(metrics["body"]["bottom"], metrics["footer"]["top"] + 1)
                    self.assertLessEqual(metrics["footer"]["bottom"], metrics["content"]["bottom"] + 1)
                    self.assertLessEqual(metrics["content"]["bottom"], viewport["height"] + 1)
                    self.assertGreater(metrics["save"]["left"], metrics["cancel"]["left"])
                    self.assertGreater(metrics["scrollHeight"], metrics["clientHeight"] + 20)
                    footer_before = footer.bounding_box()
                    body.evaluate("el => el.scrollTop = el.scrollHeight")
                    self.assertGreater(body.evaluate("el => el.scrollTop"), 0)
                    footer_after = footer.bounding_box()
                    self.assertAlmostEqual(footer_after["y"], footer_before["y"], delta=1)
                    self.assertAlmostEqual(footer_after["height"], footer_before["height"], delta=1)
        finally:
            page.close()

    def test_static_modal_footers_align_right_and_stay_inside_surface(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            ids = page.locator(".modal[id]").evaluate_all("""nodes => nodes
                .filter(el => el.querySelector(':scope > .modal-content.tech-dialog > .modal-buttons'))
                .map(el => el.id)""")
            self.assertGreaterEqual(len(ids), 25)
            for viewport in [{"width": 1440, "height": 900}, {"width": 390, "height": 844}]:
                page.set_viewport_size(viewport)
                for modal_id in ids:
                    with self.subTest(viewport=viewport, modal_id=modal_id):
                        page.evaluate("(id) => {ModalManager.closeAll(); ModalManager.open(id);}", modal_id)
                        metrics = page.locator("#" + modal_id).evaluate("""el => {
                            const content = el.querySelector(':scope > .modal-content');
                            const footer = content.querySelector(':scope > .modal-buttons');
                            const body = content.querySelector(':scope > .modal-body');
                            const r = node => node.getBoundingClientRect();
                            return {
                                display: getComputedStyle(footer).display,
                                justify: getComputedStyle(footer).justifyContent,
                                content: r(content).toJSON(), footer: r(footer).toJSON(),
                                body: body ? r(body).toJSON() : null,
                            };
                        }""")
                        if metrics["display"] == "none":
                            continue
                        self.assertEqual(metrics["justify"], "flex-end", metrics)
                        self.assertLessEqual(
                            metrics["footer"]["bottom"],
                            metrics["content"]["bottom"] + 1,
                            metrics,
                        )
                        if metrics["body"]:
                            self.assertLessEqual(
                                metrics["body"]["bottom"],
                                metrics["footer"]["top"] + 1,
                                metrics,
                            )
        finally:
            page.close()

    def test_sidebar_settings_footer_is_separate_from_scroll_and_tab_sensitive(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("ModalManager.open('sidebar-visibility-modal')")
            modal = page.locator("#sidebar-visibility-modal")
            footer = modal.locator(":scope > .modal-content > .sidebar-settings-footer")
            expect(footer).to_be_visible()
            page.evaluate("switchSidebarSettingsTab('guide')")
            expect(footer).to_be_hidden()
            page.evaluate("switchSidebarSettingsTab('visibility')")
            expect(footer).to_be_visible()
            for viewport in [{"width": 1440, "height": 900}, {"width": 390, "height": 844}]:
                page.set_viewport_size(viewport)
                before = footer.bounding_box()
                modal.locator(".modal-body").evaluate("el => el.scrollTop = el.scrollHeight")
                after = footer.bounding_box()
                self.assertAlmostEqual(before["y"], after["y"], delta=1)
                self.assertEqual(
                    footer.evaluate("el => getComputedStyle(el).justifyContent"),
                    "flex-end",
                )
        finally:
            page.close()

    def test_wifi_and_file_browser_keep_form_and_navigation_in_their_own_regions(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("ModalManager.open('wifi-modal')")
            wifi = page.locator("#wifi-modal")
            expect(wifi.locator(':scope > .modal-content > .modal-buttons')).to_be_visible()
            expect(wifi.locator('button[type="submit"][form="wifi-form"]')).to_be_visible()
            page.evaluate("ModalManager.closeAll(); ModalManager.open('file-browser-modal')")
            browser = page.locator("#file-browser-modal")
            expect(browser.locator(".file-browser-nav-actions")).to_be_visible()
            self.assertEqual(browser.locator(".file-browser-nav-actions button").count(), 3)
            self.assertEqual(
                browser.locator(":scope > .modal-content > .modal-buttons button").count(),
                2,
            )
        finally:
            page.close()

    def test_wifi_footer_submits_its_associated_form(self):
        page = self.new_page()
        requests = []

        def connect(route):
            requests.append(route.request.post_data_json)
            route.fulfill(json={"success": True})

        try:
            self.goto_shell(page)
            page.route("**/api/devices/wifi", connect)
            page.evaluate("""() => {
                state.devices = [{device_id:'fixture-device', status:'online'}];
                state.selectedDevices = new Set(['fixture-device']);
                ModalManager.open('wifi-modal');
            }""")
            page.locator('#wifi-ssid').fill('FixtureWifi')
            page.locator('#wifi-password').fill('fixture-password')
            with page.expect_response("**/api/devices/wifi"):
                page.locator('#wifi-modal button[type="submit"]').click()
            expect(page.locator('#wifi-modal')).to_be_hidden()
            self.assertEqual(requests, [{
                "devices": ["fixture-device"], "ssid": "FixtureWifi", "password": "fixture-password"}])
        finally:
            page.close()

    def test_route_header_reuses_wifi_svg(self):
        page = self.new_page()
        try:
            # This visual check must not wait for administrator elevation.
            page.route("**/api/config/read", lambda route: route.fulfill(
                json={"success": True, "ubuntu_host": "192.0.2.10"}))
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("() => { void checkRouting(); }")
            route = page.locator("#route-check-dialog")
            expect(route).to_be_visible()
            expect(route.locator(".tech-title-glyph use")).to_have_attribute("href", "/static/icons/ui.svg#wifi")
            self.assertEqual(route.locator(".tech-header-rule").count(), 1)
            self.assertEqual(route.locator(".route-check-corner").count(), 0)
            page.evaluate("ModalManager.closeAll()")
            page.evaluate("ModalManager.open('wifi-modal')")
            expect(page.locator("#wifi-modal .tech-title-glyph use")).to_have_attribute("href", "/static/icons/ui.svg#wifi")
        finally:
            page.close()
