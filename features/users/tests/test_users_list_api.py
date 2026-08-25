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
        self.assertEqual(body["users"][0]["status"], "online")
        self.assertTrue(body["users"][0]["removable"])

    def test_configured_user_without_recent_session_is_offline(self):
        import features.users.users_api as users_api

        old_config_manager = users_api.runtime.config_manager
        old_global_state = users_api.runtime.global_state
        users_api.runtime.config_manager = SimpleNamespace(
            load_config=lambda: {
                "client_hosts": {"172.16.14.80": "offline-user"},
                "vpn_gateways": [],
            }
        )
        users_api.runtime.global_state = SimpleNamespace(
            user_states={},
            user_states_lock=threading.Lock(),
        )
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
        self.assertEqual(body["users"][0]["status"], "offline")
        self.assertTrue(body["users"][0]["removable"])

    def test_only_active_cluster_owner_is_synthesized(self):
        import features.users.users_api as users_api

        old_config_manager = users_api.runtime.config_manager
        old_global_state = users_api.runtime.global_state
        users_api.runtime.config_manager = SimpleNamespace(
            load_config=lambda: {"client_hosts": {}, "vpn_gateways": []}
        )
        users_api.runtime.global_state = SimpleNamespace(
            user_states={
                "temporary-user-id": {
                    "client_username": "172.16.14.246",
                    "client_ip": "172.16.14.246",
                    "display_client_id": "172.16.14.246@172.16.14.246",
                    "running": False,
                    "devices": [],
                    "last_seen": datetime.now().isoformat(),
                    "created_at": datetime.now().isoformat(),
                }
            },
            user_states_lock=threading.Lock(),
        )
        jobs = [
            {
                "id": "completed-job",
                "owner_id": "historical-owner",
                "status": "completed",
                "created_at": "2026-08-01T10:00:00",
                "updated_at": "2026-08-01T11:00:00",
                "leases": [],
            },
            {
                "id": "active-job",
                "owner_id": "active-owner",
                "status": "running",
                "created_at": "2026-08-25T10:00:00",
                "updated_at": "2026-08-25T10:01:00",
                "assigned_worker_id": "worker-1",
                "leases": [],
            },
        ]
        cluster = SimpleNamespace(
            repository=SimpleNamespace(list_jobs=lambda limit: jobs)
        )
        try:
            with (
                patch(
                    "features.users.cluster_access.get_cluster_service",
                    return_value=cluster,
                ),
                patch.object(
                    users_api,
                    "resolve_client_display_id",
                    side_effect=lambda owner_id: owner_id,
                ),
            ):
                resp = asyncio.run(users_api.list_users())
        finally:
            users_api.runtime.config_manager = old_config_manager
            users_api.runtime.global_state = old_global_state

        body = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(body["total"], 2)
        users = {item["client_id"]: item for item in body["users"]}
        self.assertEqual(users["active-owner"]["status"], "testing")
        self.assertFalse(users["active-owner"]["removable"])
        self.assertIn("正在测试", users["active-owner"]["removal_reason"])
        temporary = users["172.16.14.246@172.16.14.246"]
        self.assertEqual(temporary["status"], "online")
        self.assertFalse(temporary["removable"])
        self.assertIn("临时在线会话", temporary["removal_reason"])


if __name__ == "__main__":
    unittest.main()
