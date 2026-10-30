"""Firmware browsing uses server defaults instead of the browser return address."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class FirmwareShareHostBrowserTests(RuntimeUiHarness):
    def test_browse_resolves_server_config_and_preserves_explicit_share_hosts(self):
        page = self.new_page()
        client_return = "windows-user@192.0.2.66"
        config = {"local_server": client_return, "ubuntu_host": "192.0.2.233", "ubuntu_user": "worker-user"}
        listings = []
        config_reads = []

        def config_handler(route):
            self.assertEqual(route.request.method, "GET")
            config_reads.append(True)
            route.fulfill(content_type="application/json", body=json.dumps(config))

        def browse_handler(route):
            body = route.request.post_data_json
            listings.append(body)
            route.fulfill(content_type="application/json", body=json.dumps({
                "success": True, "data": {
                    "host": body["host"], "user": body["user"], "path": body["path"] or "/home/worker-user",
                    "files": [{"name": "firmware.img", "type": "file", "size": 4096}],
                },
            }))

        page.route("**/api/config/read", config_handler)
        page.route("**/api/firmware-shares/browse", browse_handler)
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof browseRemoteFileForFirmwareShare === 'function'")
            page.evaluate("client => { state.config = {local_server: client, effective_ubuntu_user: 'worker-user'}; }", client_return)
            expected = [
                {"host": "192.0.2.233", "user": "worker-user", "path": ""},
                {"host": "192.0.2.206", "user": "build-user", "path": "/home/build"},
                {"host": "192.0.2.207", "user": "remote-user", "path": "/home/remote"},
            ]
            for index, target in enumerate(expected):
                if index == 1:
                    config["firmware_shares"] = {"default_host": "build-user@192.0.2.206", "default_path": "/home/build"}
                elif index == 2:
                    config["firmware_shares"] = {
                        "default_remote": "remote-user@192.0.2.207:/home/remote",
                        "default_host": "other-user@192.0.2.208", "default_path": "/home/other",
                    }
                page.evaluate("async () => { await browseRemoteFileForFirmwareShare(); }")
                expect(page.locator("#file-browser-modal")).to_be_visible()
                expect(page.locator("#file-browser-list")).to_contain_text("firmware.img")
                self.assertEqual(listings[-1], target)
                page.locator("#file-browser-list .file-browser-item").dblclick()
                expect(page.locator("#file-browser-modal")).to_be_hidden()
                directory = target["path"] or "/home/worker-user"
                expect(page.locator("#firmware-share-remote")).to_have_value(
                    f"{target['user']}@{target['host']}:{directory}/firmware.img"
                )
            self.assertEqual(len(config_reads), 3)

            config.clear()
            config["local_server"] = client_return
            page.evaluate("async () => { await browseRemoteFileForFirmwareShare(); }")
            self.assertEqual(len(listings), 3)
            expect(page.locator("#file-browser-modal")).to_be_hidden()
        finally:
            page.close()
