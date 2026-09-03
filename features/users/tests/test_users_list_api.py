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
                    side_effect=lambda owner_id, stored="": owner_id,
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

    def test_local_devices_attached_for_user_hosts(self):
        import features.users.users_api as users_api

        old_config_manager = users_api.runtime.config_manager
        old_global_state = users_api.runtime.global_state
        users_api.runtime.config_manager = SimpleNamespace(
            load_config=lambda: {
                "client_hosts": {"172.16.14.65": "cp2-share"},
                "vpn_gateways": [],
            }
        )
        users_api.runtime.global_state = SimpleNamespace(
            user_states={}, user_states_lock=threading.Lock(),
        )
        inventory = {
            "devices": ["SERIAL1"],
            "source_os": "windows",
            "available": True,
            "error": "",
        }
        try:
            with patch(
                "features.users.cluster_access.get_cluster_service",
                side_effect=RuntimeError("cluster not configured in unit test"),
            ), patch.object(
                users_api,
                "host_local_device_inventory",
                return_value=inventory,
            ) as mock_inventory:
                resp = asyncio.run(users_api.list_users())
        finally:
            users_api.runtime.config_manager = old_config_manager
            users_api.runtime.global_state = old_global_state

        body = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(body["total"], 1)
        self.assertEqual(
            body["users"][0]["local_devices"], inventory,
        )
        mock_inventory.assert_called_once_with("cp2-share@172.16.14.65")

    def test_platform_account_session_merges_into_user_row(self):
        """认证 API/CLI 会话以内部 user_id 作为状态 key（无 @ip、无
        client_username/client_ip），必须解析回平台身份并入对应用户行，
        而不是显示成一串裸 token + unknown/未知。"""
        import features.users.users_api as users_api

        old_config_manager = users_api.runtime.config_manager
        old_global_state = users_api.runtime.global_state
        users_api.runtime.config_manager = SimpleNamespace(
            load_config=lambda: {
                "client_hosts": {"172.16.14.233": "hcq@172.16.14.233"},
                "vpn_gateways": [],
            }
        )
        users_api.runtime.global_state = SimpleNamespace(
            user_states={
                "UenosBYxAtzselkIfGqb-w": {
                    "running": False,
                    "devices": [],
                    "last_seen": datetime.now().isoformat(),
                    "created_at": datetime.now().isoformat(),
                },
            },
            user_states_lock=threading.Lock(),
        )
        try:
            with patch(
                "features.users.cluster_access.get_cluster_service",
                side_effect=RuntimeError("cluster not configured in unit test"),
            ), patch(
                "features.users.users_api.resolve_client_display_id",
                return_value="hcq@172.16.14.233",
            ) as mock_resolve, patch.object(
                users_api,
                "host_local_device_inventory",
                return_value={"devices": [], "available": False, "error": ""},
            ):
                resp = asyncio.run(users_api.list_users())
        finally:
            users_api.runtime.config_manager = old_config_manager
            users_api.runtime.global_state = old_global_state

        body = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(body["total"], 1)
        row = body["users"][0]
        self.assertEqual(row["client_id"], "hcq@172.16.14.233")
        self.assertEqual(row["username"], "hcq@172.16.14.233")
        self.assertEqual(row["ip"], "172.16.14.233")
        self.assertEqual(row["source"], "internal")
        self.assertEqual(row["status"], "online")
        mock_resolve.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class UsersListLockSafetyTests(unittest.TestCase):
    """2026-09-03 自死锁回归：list_users 不得在持有 user_states_lock 的
    同时调用 resolve_client_display_id（内部会再次申请同一把非重入锁，
    事件循环同线程自死锁，看门狗 dump 实锤）。"""

    def test_resolve_is_called_without_holding_user_states_lock(self):
        import features.users.users_api as users_api

        old_config_manager = users_api.runtime.config_manager
        old_global_state = users_api.runtime.global_state
        users_api.runtime.config_manager = SimpleNamespace(
            load_config=lambda: {"client_hosts": {}, "vpn_gateways": []}
        )
        users_api.runtime.global_state = SimpleNamespace(
            user_states={
                # 无 @ 的平台 user_id 状态：触发 resolve_client_display_id 路径。
                "PlatformUserIdNoAtSign": {
                    "running": False,
                    "devices": [],
                    "last_seen": datetime.now().isoformat(),
                },
            },
            user_states_lock=threading.Lock(),
        )

        lock_held_during_resolve = []

        def spying_resolve(client_id, stored=""):
            # 若死锁 bug 回归，这里会在锁内被调用——用非阻塞获取检测。
            acquired = users_api.runtime.global_state.user_states_lock.acquire(
                blocking=False
            )
            lock_held_during_resolve.append(not acquired)
            if acquired:
                users_api.runtime.global_state.user_states_lock.release()
            return f"resolved@1.2.3.4" if client_id else ""

        try:
            with patch(
                "features.users.cluster_access.get_cluster_service",
                side_effect=RuntimeError("cluster not configured in unit test"),
            ), patch.object(
                users_api,
                "resolve_client_display_id",
                side_effect=spying_resolve,
            ), patch.object(
                users_api,
                "host_local_device_inventory",
                return_value={"devices": [], "available": False, "error": ""},
            ):
                resp = asyncio.run(users_api.list_users())
        finally:
            users_api.runtime.config_manager = old_config_manager
            users_api.runtime.global_state = old_global_state

        body = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(body["total"], 1)
        self.assertTrue(lock_held_during_resolve, "resolve 应被调用")
        self.assertFalse(
            any(lock_held_during_resolve),
            "resolve 被调用时不得持有 user_states_lock（否则非重入锁自死锁）",
        )
