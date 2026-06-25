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
            response = asyncio.run(config_api.update_config({"sidebar_order": ["test"]}))
        finally:
            config_api.config_manager = old_manager

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertEqual(fake.saved["sidebar_order"], ["test"])
        self.assertEqual(fake.saved["gerrit_dashboard"]["ssh_host"], "10.10.10.29")
        self.assertEqual(fake.saved["redmine_auth"]["username"], "chaoqun.huang@rock-chips.com")

    def test_client_username_save_uses_preserving_runtime_merge(self):
        from features.users.sessions import ClientManager

        class FakeConfigManager:
            def __init__(self):
                self.prepare_updates = None
                self.saved = None

            def prepare_client_config(self, updates):
                self.prepare_updates = updates
                return {
                    "gerrit_dashboard": {"ssh_host": "10.10.10.29"},
                    **updates,
                }

            def save_runtime_config(self, data):
                self.saved = data
                return True

        fake = FakeConfigManager()
        manager = ClientManager()
        manager.config_manager = fake
        manager.client_hosts = {"172.16.14.66": "hcq"}
        manager.ssh_credentials = [{"username": "hcq", "password": "secret"}]

        self.assertTrue(manager._save_client_runtime())
        self.assertEqual(fake.prepare_updates["client_hosts"]["172.16.14.66"], "hcq")
        self.assertEqual(fake.saved["gerrit_dashboard"]["ssh_host"], "10.10.10.29")


if __name__ == "__main__":
    unittest.main()
