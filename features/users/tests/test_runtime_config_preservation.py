import asyncio
import json
import unittest


class RuntimeConfigPreservationTests(unittest.TestCase):
    def test_update_config_preserves_gerrit_runtime_section(self):
        import features.users.config_api as config_api

        class FakeConfigManager:
            def __init__(self):
                self.saved = None

            def get_runtime_config(self):
                return {
                    "client_hosts": {"172.16.14.66": "hcq"},
                    "gerrit_dashboard": {"ssh_host": "10.10.10.29"},
                    "redmine_auth": {"username": "chaoqun.huang@rock-chips.com"},
                }

            def save_runtime_config(self, data):
                self.saved = data
                return True

        fake = FakeConfigManager()
        old_manager = config_api.config_manager
        try:
            config_api.config_manager = fake
            response = asyncio.run(config_api.update_config({
                "client_hosts": {"172.16.14.67": "alice"},
            }))
        finally:
            config_api.config_manager = old_manager

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertEqual(fake.saved["client_hosts"], {"172.16.14.67": "alice"})
        self.assertEqual(fake.saved["gerrit_dashboard"]["ssh_host"], "10.10.10.29")
        self.assertEqual(fake.saved["redmine_auth"]["username"], "chaoqun.huang@rock-chips.com")

    def test_client_username_save_uses_preserving_runtime_merge(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from features.users.sessions import ClientManager
        from foundation.config import ConfigManager

        with TemporaryDirectory() as tmp:
            config = ConfigManager(project_root=Path(tmp))
            config.update_runtime_config({
                "gerrit_dashboard": {"ssh_host": "gerrit.example"},
                "client_hosts": {"192.0.2.1": "old"},
                "client_ssh_credentials": [{"username": "new", "encrypted_password": "saved"}],
            })
            manager = ClientManager()
            manager.config_manager = config
            manager.client_hosts = {"192.0.2.2": "alice"}
            manager.ssh_credentials = [{"username": "stale", "password": "stale"}]
            self.assertTrue(manager._save_client_runtime("192.0.2.2"))
            saved = config.get_runtime_config()
            self.assertEqual(saved["client_hosts"], {"192.0.2.1": "old", "192.0.2.2": "alice"})
            self.assertEqual(saved["client_ssh_credentials"][0]["username"], "new")
            self.assertEqual(saved["gerrit_dashboard"]["ssh_host"], "gerrit.example")
