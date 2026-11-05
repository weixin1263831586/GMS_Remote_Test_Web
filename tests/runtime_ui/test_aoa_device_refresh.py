"""AOA inventories follow reconnects without moving the selected or bound target."""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaDeviceRefreshTests(RuntimeUiHarness):
    def prepare(self, page, inventory):
        reads, writes = [], []

        def management(route):
            reads.append(route.request.url)
            route.fulfill(content_type='application/json', body=json.dumps({
                'success': True, 'devices': inventory, 'groups': []}))

        def aoa(route):
            if route.request.method != 'GET':
                writes.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body='{"success":true,"profiles":[]}')

        page.route('**/api/devices/management*', management)
        page.route('**/api/devices/groups', lambda route: route.fulfill(
            content_type='application/json', body='{"success":true,"groups":[]}'))
        page.route('**/api/devices/aoa/**', aoa)
        self.goto_shell(page)
        page.evaluate("async () => {switchPage('devices'); await aoaViewToggle(true);}")
        page.locator('#aoa-replay-worker').select_option('ats-worker-controller')
        page.locator('#aoa-replay-device').select_option('RK3576GMS1')
        return reads, writes

    @staticmethod
    def device(serial, model='Box'):
        return {'serial_no': serial, 'worker_id': 'ats-worker-controller',
                'transport': 'local_usb', 'status': 'available', 'model': model}

    def test_poll_recovers_rebooted_target_without_switching_to_other_device(self):
        page = self.new_page()
        inventory = [self.device('OTHER'), self.device('RK3576GMS1')]
        try:
            page.clock.install()
            reads, writes = self.prepare(page, inventory)
            page.locator('#aoa-record-device').evaluate("node => {node.value = 'RK3576GMS1';}")
            assert page.evaluate('devicesRefreshInterval !== null')
            inventory[:] = [self.device('OTHER')]
            count = len(reads)
            page.clock.fast_forward(10010)
            expect(page.locator('#aoa-replay-device')).to_have_value('RK3576GMS1')
            expect(page.locator('#aoa-replay-device option:checked')).to_contain_text('暂未枚举')
            expect(page.locator('#aoa-record-device')).to_have_value('RK3576GMS1')
            assert len(reads) > count
            inventory[:] = [self.device('OTHER'), self.device('RK3576GMS1', 'SSI_17_on_ARM64'),
                            self.device('NEW-USB')]
            page.clock.fast_forward(10010)
            expect(page.locator('#aoa-replay-device option:checked')).to_have_text('RK3576GMS1 · SSI_17_on_ARM64')
            expect(page.locator('#aoa-replay-device option[value="NEW-USB"]')).to_have_count(1)
            # An identical inventory keeps the same native option nodes.
            page.evaluate("window.selectedTargetOption = aoaEl('aoa-replay-device').selectedOptions[0]")
            page.clock.fast_forward(10010)
            page.wait_for_function("selectedTargetOption === aoaEl('aoa-replay-device').selectedOptions[0]")
            assert writes == []
            page.evaluate("switchPage('reports')")
            page.wait_for_function('devicesRefreshInterval === null')
        finally:
            page.close()

    def test_worker_event_refreshes_inventory_and_preserves_hid_binding(self):
        page = self.new_page()
        inventory = [self.device('OTHER'), self.device('RK3576GMS1')]
        try:
            page.clock.install()
            reads, writes = self.prepare(page, inventory)
            page.evaluate("""Object.assign(aoaDebugState, {
                id:'debug-session', worker:'ats-worker-controller', device:'RK3576GMS1'
            }); aoaDebugButtonState();""")
            inventory[:] = [self.device('OTHER')]
            count = len(reads)
            page.evaluate("handleServerEvent('worker.availability_changed', {worker_id:'ats-worker-controller',status:'offline'})")
            page.clock.fast_forward(300)
            expect(page.locator('#aoa-replay-device option:checked')).to_contain_text('暂未枚举')
            expect(page.locator('#aoa-replay-device')).to_have_value('RK3576GMS1')
            expect(page.locator('#aoa-replay-device')).to_be_disabled()
            assert len(reads) > count
            assert page.evaluate('aoaDebugState.device') == 'RK3576GMS1'
            assert writes == []
            page.evaluate('aoaDebugReset()')
        finally:
            page.close()
