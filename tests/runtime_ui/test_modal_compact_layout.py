"""Endpoint rows and preflight cards leave space for the actual device content."""
from __future__ import annotations

import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness, expect


VIEWPORTS = [
    {'width': 1440, 'height': 960}, {'width': 768, 'height': 720},
    {'width': 390, 'height': 844}, {'width': 844, 'height': 390},
]


class CompactModalLayoutTests(RuntimeUiHarness):
    def capture(self, page, name):
        if output := os.getenv('GMS_MODAL_GALLERY_OUT'):
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f'compact-{name}-{page.viewport_size["width"]}.png'), animations='disabled')

    def test_horizontal_dialog_labels_fit_content_and_align(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            for modal_id in ("elevate-modal", "username-detect-modal", "device-password-modal",
                             "wifi-modal", "vpn-credential-modal", "firmware-share-password-modal",
                             "extract-suite-modal"):
                with self.subTest(modal=modal_id):
                    page.set_viewport_size({"width": 1440, "height": 960})
                    page.evaluate("id => ModalManager.open(id)", modal_id)
                    group = page.locator(f"#{modal_id} .tech-aligned-fields")
                    expect(group).to_be_visible()
                    geometry = group.evaluate("""el => [...el.querySelectorAll(':scope > .tech-field-card')].map(row => {
                      const label = row.querySelector(':scope > label');
                      const field = row.querySelector(':scope > input:not([type=hidden]),:scope > select,:scope > .password-input-wrap');
                      const range = document.createRange();
                      range.selectNodeContents(label);
                      const text = range.getBoundingClientRect();
                      return {textRight:text.right, textWidth:text.width,
                              labelRight:label.getBoundingClientRect().right,
                              inputLeft:field.getBoundingClientRect().left,
                              inputWidth:field.getBoundingClientRect().width};
                    })""")
                    self.assertTrue(geometry)
                    for row in geometry:
                        self.assertGreater(row["inputWidth"], 80)
                        self.assertLessEqual(row["labelRight"], row["inputLeft"] + 1)
                        self.assertAlmostEqual(row["inputLeft"], geometry[0]["inputLeft"], delta=2)
                    longest = max(geometry, key=lambda row: row["textWidth"])
                    self.assertLessEqual(longest["inputLeft"] - longest["textRight"], 18, geometry)
                    self.capture(page, modal_id + "-labels")
                    page.evaluate("id => ModalManager.close(id)", modal_id)
        finally:
            page.close()

    def test_routing_endpoints_share_a_row_and_leave_inventory_accessible(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            for kind, target_id, devices_id, assignments_id in [
                ('adb-proxy', 'adb-proxy-target-host', 'adb-proxy-source-devices', 'adb-proxy-assignments'),
                ('usbip-attach', 'usbip-target-worker', 'usbip-source-device', 'usbip-assignments'),
            ]:
                source_id = 'adb-proxy-source-host' if kind == 'adb-proxy' else 'usbip-source-host'
                page.evaluate('''({sourceId,targetId,devicesId,assignmentsId}) => {
                    document.getElementById(sourceId).replaceChildren(new Option('fixture@192.0.2.20 · 设备源', 'source'));
                    document.getElementById(targetId).replaceChildren(new Option('fixture-worker · 接入主机', 'target'));
                    document.getElementById(devicesId).replaceChildren(...Array.from({length:8}, (_,i) =>
                        new Option(`USB ${i + 1} · Android rendering fixture`, `fixture-${i}`)));
                    const rows = Array.from({length:8}, (_,i) => {
                        const row = document.createElement('div');
                        row.className = 'adb-proxy-assignment';
                        const info = document.createElement('div');
                        info.className = 'adb-proxy-assignment-info';
                        info.textContent = `fixture-source → fixture-worker · USB ${i + 1}`;
                        const actions = document.createElement('div');
                        actions.className = 'device-routing-actions';
                        const disconnect = document.createElement('button');
                        disconnect.type = 'button';
                        disconnect.className = 'btn-xxs btn-danger';
                        disconnect.textContent = '断开';
                        disconnect.disabled = true;
                        actions.append(disconnect);
                        row.append(info, actions);
                        return row;
                    });
                    document.getElementById(assignmentsId).replaceChildren(...rows);
                }''', {'sourceId': source_id, 'targetId': target_id, 'devicesId': devices_id, 'assignmentsId': assignments_id})
                for viewport in VIEWPORTS:
                    with self.subTest(kind=kind, viewport=viewport):
                        page.set_viewport_size(viewport)
                        page.evaluate('id => ModalManager.open(id)', kind + '-modal')
                        modal = page.locator(f'#{kind}-modal')
                        source = page.locator('#' + source_id).bounding_box()
                        target = page.locator('#' + target_id).bounding_box()
                        self.assertAlmostEqual(source['y'], target['y'], delta=1)
                        self.assertGreaterEqual(target['x'], source['x'] + source['width'])
                        self.assertGreater(source['width'], 100)
                        devices = page.locator('#' + devices_id)
                        self.assertGreaterEqual(devices.bounding_box()['height'], 100)
                        current = page.locator('#' + assignments_id)
                        expect(current.locator('.adb-proxy-assignment-info').first).to_have_css('font-size', '13px')
                        body = modal.locator('.modal-body')
                        body.evaluate('el => el.scrollTop = 0')
                        if viewport['height'] >= 720:
                            body_box = body.bounding_box()
                            self.assertLessEqual(devices.bounding_box()['y'] + devices.bounding_box()['height'], body_box['y'] + body_box['height'] + 1)
                            self.assertLess(current.bounding_box()['y'] + 40, body_box['y'] + body_box['height'])
                        current.scroll_into_view_if_needed()
                        current.evaluate('el => el.scrollTop = el.scrollHeight')
                        self.assertGreater(current.evaluate('el => el.scrollTop'), 0)
                        expect(modal.locator('.modal-buttons button').last).to_be_in_viewport()
                        body.evaluate('el => el.scrollTop = 0')
                        current.evaluate('el => el.scrollTop = 0')
                        self.capture(page, kind)
                        page.evaluate('id => ModalManager.close(id)', kind + '-modal')
        finally:
            page.close()

    def test_firmware_empty_fields_have_no_reserved_blank_rows(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate('''() => {
                document.getElementById('firmware-target-devices').textContent = 'fixture-device-01';
                document.getElementById('firmware-path').value = '';
                ModalManager.open('firmware-modal');
            }''')
            modal = page.locator('#firmware-modal')
            for viewport in VIEWPORTS:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    for card_selector, field_selector in [
                        ('.firmware-target-card', '#firmware-target-devices'),
                        ('.firmware-artifact-card', '#firmware-path'),
                    ]:
                        card = modal.locator(card_selector).bounding_box()
                        field = modal.locator(field_selector).bounding_box()
                        self.assertLessEqual(card['y'] + card['height'] - field['y'] - field['height'], 14)
                    for button in modal.locator('.modal-buttons button').all():
                        expect(button).to_have_css('font-size', '12px')
                        self.assertLessEqual(button.bounding_box()['height'], 34)
                    expect(modal.locator('.modal-title')).to_have_css('font-size', '16px')
                    self.capture(page, 'firmware')
            self.assertEqual(page.locator('#firmware-file-meta').count(), 0)
            # The field itself must retain its height for both local files and
            # server paths; no auxiliary metadata row may resize the dialog.
            for viewport in VIEWPORTS:
                with self.subTest(selected_file_viewport=viewport):
                    page.set_viewport_size(viewport)
                    page.locator('#firmware-path').fill('')
                    before_modal = modal.bounding_box()['height']
                    before_card = modal.locator('.firmware-artifact-card').bounding_box()['height']
                    page.locator('#firmware-path').fill('/tmp/rendering-fixture.img')
                    self.assertAlmostEqual(modal.bounding_box()['height'], before_modal, delta=1)
                    self.assertAlmostEqual(
                        modal.locator('.firmware-artifact-card').bounding_box()['height'],
                        before_card, delta=1,
                    )
                    # Simulate native file selection without actually uploading.
                    page.evaluate("""() => {
                        const id = 'firmware-file-input';
                        let input = document.getElementById(id);
                        if (!input) {
                            input = document.createElement('input');
                            input.type = 'file';
                            input.id = id;
                            input.hidden = true;
                            document.body.append(input);
                        }
                        const transfer = new DataTransfer();
                        transfer.items.add(new File(['fixture'], 'update.img'));
                        input.files = transfer.files;
                        document.getElementById('firmware-path').value = 'update.img';
                    }""")
                    self.assertAlmostEqual(modal.bounding_box()['height'], before_modal, delta=1)
                    self.assertAlmostEqual(
                        modal.locator('.firmware-artifact-card').bounding_box()['height'],
                        before_card, delta=1,
                    )
        finally:
            page.close()
