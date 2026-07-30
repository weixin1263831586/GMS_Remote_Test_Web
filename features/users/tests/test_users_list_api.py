import asyncio
import json
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch


class UsersListApiTests(unittest.TestCase):
    def test_display_identity_does_not_repeat_same_ip(self):
        import features.users.clients as clients

        self.assertEqual(
            clients.format_client_display_id(
                "hcq@172.16.14.66",
                "172.16.14.66",
            ),
            "hcq@172.16.14.66",
        )
        self.assertEqual(
            clients.normalize_client_display_id(
                "hcq@172.16.14.66@172.16.14.66"
            ),
            "hcq@172.16.14.66",
        )

    def test_internal_account_id_resolves_to_user_management_identity(self):
        import features.users.clients as clients

        old_config_manager = clients.runtime.config_manager
        old_global_state = clients.runtime.global_state
        clients.runtime.config_manager = SimpleNamespace(
            load_config=lambda: {"client_hosts": {"172.16.14.66": "hcq"}}
        )
        clients.runtime.global_state = SimpleNamespace(
            user_states={
                "N387pLbIBhpMw5JsWUL9hg": {
                    "client_username": "hcq",
                    "client_ip": "172.16.14.66",
                    "display_client_id": "hcq@172.16.14.66",
                },
            },
            user_states_lock=threading.Lock(),
        )
        try:
            with patch(
                "features.auth.auth_service.list_users",
                return_value=[{
                    "id": "N387pLbIBhpMw5JsWUL9hg",
                    "username": "hcq",
                }],
            ):
                self.assertEqual(
                    clients.resolve_client_display_id(
                        "N387pLbIBhpMw5JsWUL9hg"
                    ),
                    "hcq@172.16.14.66",
                )
                self.assertEqual(
                    clients.resolve_client_display_id(
                        "N387pLbIBhpMw5JsWUL9hg",
                        "hcq@172.16.14.66@172.16.14.66",
                    ),
                    "hcq@172.16.14.66",
                )
        finally:
            clients.runtime.config_manager = old_config_manager
            clients.runtime.global_state = old_global_state

    def test_configured_active_user_keeps_configured_flag(self):
        import features.users.users_api as users_api

        old_config_manager = users_api.runtime.config_manager
        old_global_state = users_api.runtime.global_state

        class FakeConfigManager:
            def load_config(self):
                return {
                    "client_hosts": {
                        "172.16.14.65": "cp2-share",
                    },
                    "vpn_gateways": [],
                }

        fake_state = SimpleNamespace(
            user_states={
                "cp2-share@172.16.14.65": {
                    "client_username": "cp2-share",
                    "client_ip": "172.16.14.65",
                    "display_client_id": "cp2-share@172.16.14.65",
                    "running": False,
                    "devices": [],
                    "last_seen": datetime.now().isoformat(),
                    "created_at": datetime.now().isoformat(),
                }
            },
            user_states_lock=threading.Lock(),
        )

        users_api.runtime.config_manager = FakeConfigManager()
        users_api.runtime.global_state = fake_state
        try:
            with patch(
                "features.cluster.get_cluster_service",
                side_effect=RuntimeError("cluster not configured in unit test"),
            ):
                resp = asyncio.run(users_api.list_users())
        finally:
            users_api.runtime.config_manager = old_config_manager
            users_api.runtime.global_state = old_global_state

        body = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["users"][0]["client_id"], "cp2-share@172.16.14.65")
        self.assertTrue(body["users"][0]["configured"])


if __name__ == "__main__":
    unittest.main()
