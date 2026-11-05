"""A measured calibration must visibly report whether Controller persistence worked."""
import json

from playwright.sync_api import expect

from tests.aoa_fixtures import verified_mapping
from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaMappingSaveTests(RuntimeUiHarness):
    def test_calibration_save_failure_is_visible_and_retry_reports_record_id(self):
        page = self.new_page()
        mapping = verified_mapping().to_dict()
        failures = [True]
        def reply(route):
            if route.request.url.endswith('/record/input'):
                payload = {"success": True, "touch_calibration": {"measured": True,
                    "mapping": mapping, "context": mapping["context"], "scale_x": 2.2, "scale_y": 2}}
                status = 200
            elif route.request.url.endswith('/calibrations'):
                status = 503 if failures[0] else 200
                payload = {"detail": "storage unavailable"} if failures[0] else {"success": True, "calibration": {"id": "saved-record"}}
            else:
                status, payload = 200, {"success": True, "profiles": []}
            route.fulfill(status=status, content_type='application/json', body=json.dumps(payload))
        try:
            page.route('**/api/devices/aoa/**', reply)
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices');
                window.aoaLiveScreen = {start: async () => {}, stop: () => {}, isActive: () => true};
                await aoaViewToggle(true);
                aoaSyncDeviceSources([{serial_no:'REC', worker_id:'w', state:'available'}]);
                await aoaExecuteRecorderInput({action:'calibrate'}, {refresh:false});
            }""")
            expect(page.locator('#aoa-calibration-status')).to_contain_text('保存失败')
            failures[0] = False
            page.evaluate("aoaExecuteRecorderInput({action:'calibrate'}, {refresh:false})")
            expect(page.locator('#aoa-calibration-status')).to_contain_text('已保存 saved-record')
        finally:
            page.close()
