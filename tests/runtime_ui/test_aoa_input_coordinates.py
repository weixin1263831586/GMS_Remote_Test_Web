"""Video and screenshot clicks carry their own coordinate space to the Worker."""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaInputCoordinatesTests(RuntimeUiHarness):
    def prepare(self, page, inputs):
        def reply(route):
            if route.request.url.endswith('/record/input'):
                inputs.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({
                'success': True, 'profiles': [], 'command': 'click 200 540'}))

        page.route('**/api/devices/aoa/**', reply)
        for path in ('**/api/devices/management*', '**/api/cluster/devices*'):
            page.route(path, lambda route: route.fulfill(
                content_type='application/json', body='{"success":true,"devices":[]}'))
        self.goto_shell(page)
        page.evaluate("""async () => {
            switchPage('devices'); await aoaViewToggle(true);
            window.testLive = true;
            window.aoaLiveScreen = {start: async () => {}, stop: () => {}, isActive: () => window.testLive};
            aoaSyncDeviceSources([{serial_no:'REC',worker_id:'worker-a',transport:'local_usb',state:'available'}]);
            window.loadDevicesManagement = async () => {};
            aoaPanelState.liveTargetKey = 'worker-a:REC';
            const canvas = aoaEl('aoa-screen-canvas');
            canvas.width = 576; canvas.height = 1024; canvas.hidden = false;
            aoaEl('aoa-screen-image').hidden = true; aoaEl('aoa-screen-empty').hidden = true;
        }""")

    def test_video_click_tracks_pixel_position_across_layout_sizes(self):
        page, inputs = self.new_page(), []
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            self.prepare(page, inputs)
            for width, height in ((1920, 1080), (1366, 768)):
                page.evaluate("switchPage('reports'); switchPage('devices'); aoaViewToggle(true)")
                page.set_viewport_size({'width': width, 'height': height})
                page.locator('#aoa-screen-canvas').click(position={
                    'x': page.locator('#aoa-screen-canvas').bounding_box()['width'] * 320 / 576,
                    'y': page.locator('#aoa-screen-canvas').bounding_box()['height'] * 864 / 1024})
                expect(page.locator('#aoa-run-status')).to_have_text('操作完成')
                action = inputs[-1]['input']
                assert action['action'] == 'tap'
                assert action['frame_size'] == [576, 1024]
                assert abs(action['x'] - 320) <= 1
                assert abs(action['y'] - 864) <= 1
                assert inputs[-1]['worker_id'] == 'worker-a'
                assert inputs[-1]['device'] == 'REC'
            assert len(inputs) == 2
            assert errors == []
        finally:
            page.close()

    def test_live_stream_waits_for_first_frame_and_clears_readiness_on_stop(self):
        page = self.new_page()
        try:
            page.route('**/api/devices/aoa/**', lambda route: route.fulfill(
                content_type='application/json', body='{"success":true,"profiles":[]}'))
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices'); await aoaViewToggle(true);
                window.VideoDecoder = class {};
                window.WebSocket = class {
                    constructor() { window.testVideoSocket = this; }
                    close() {}
                };
                await window.aoaLiveScreen.start('REC', 'worker-a');
                window.testVideoSocket.onopen();
            }""")
            assert page.evaluate('aoaLiveScreen.activeSurface() === null')
            assert page.evaluate('aoaPointFromPointer({clientX:300,clientY:400}) === null')
            # A decoded frame marks the canvas ready; stopping invalidates it.
            page.evaluate("aoaEl('aoa-screen-canvas').dataset.aoaFrameReady = '1'")
            assert page.evaluate('aoaLiveScreen.activeSurface() === aoaEl("aoa-screen-canvas")')
            page.evaluate('aoaLiveScreen.stop()')
            assert page.evaluate('aoaLiveScreen.activeSurface() === null')
            assert page.evaluate('!aoaEl("aoa-screen-canvas").dataset.aoaFrameReady')
        finally:
            page.close()

    def test_bars_cancelled_gestures_and_frame_changes_send_no_input(self):
        page, inputs = self.new_page(), []
        try:
            self.prepare(page, inputs)
            # Force object-fit bars *inside* the canvas element's bounding box.
            point = page.evaluate("""() => {
                const canvas = aoaEl('aoa-screen-canvas');
                canvas.style.width = '100%'; canvas.style.height = '100%';
                const box = canvas.getBoundingClientRect();
                return {bar:{x:box.left+1,y:box.top+box.height/2},
                    center:{x:box.left+box.width/2,y:box.top+box.height/2}};
            }""")
            page.mouse.click(point['bar']['x'], point['bar']['y'])
            assert inputs == []
            page.mouse.move(point['center']['x'], point['center']['y'])
            page.mouse.down()
            page.locator('#aoa-screen-canvas').dispatch_event('pointercancel', {'pointerId': 1})
            page.mouse.up()
            assert inputs == []
            page.mouse.down()
            page.evaluate("aoaEl('aoa-screen-canvas').width = 1024; aoaEl('aoa-screen-canvas').height = 576")
            page.mouse.up()
            assert inputs == []
        finally:
            page.close()

    def test_long_press_swipe_and_native_screenshot_include_frame_size(self):
        page, inputs = self.new_page(), []
        try:
            self.prepare(page, inputs)
            box = page.locator('#aoa-screen-canvas').bounding_box()
            x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            page.mouse.click(x, y, delay=720)
            expect(page.locator('#aoa-run-status')).to_have_text('操作完成')
            assert inputs[-1]['input']['action'] == 'long_click'
            assert inputs[-1]['input']['frame_size'] == [576, 1024]
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x, y - 80, steps=5)
            page.mouse.up()
            expect(page.locator('#aoa-run-status')).to_have_text('操作完成')
            assert inputs[-1]['input']['action'] == 'swipe'
            assert inputs[-1]['input']['frame_size'] == [576, 1024]
            page.evaluate("""async () => {
                window.testLive = false; aoaEl('aoa-screen-canvas').hidden = true;
                const image = aoaEl('aoa-screen-image');
                image.src = 'data:image/svg+xml,' + encodeURIComponent(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="2560"></svg>');
                await image.decode(); image.hidden = false;
            }""")
            page.locator('#aoa-screen-image').click()
            expect(page.locator('#aoa-run-status')).to_have_text('操作完成')
            action = inputs[-1]['input']
            assert action['frame_size'] == [1440, 2560]
            assert abs(action['x'] - 720) <= 1 and abs(action['y'] - 1280) <= 1
            assert len(inputs) == 3
        finally:
            page.close()
