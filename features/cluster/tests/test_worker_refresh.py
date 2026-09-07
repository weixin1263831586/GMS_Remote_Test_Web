"""POST /workers/{id}/refresh 冷启动注册等待的回归测试。

回归：17f1490 新增的 refresh 端点在 get_worker 为 None 时立即 404，
而本地 Worker 由 LocalWorkerBridge 在启动 ~1s 心跳延迟后才注册，
冷启动窗口内所有刷新请求必然失败。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api as cluster_api_mod
from features.cluster import devices_api
from features.cluster.repository import ClusterRepository


class RefreshWorkerInventoryTests(unittest.TestCase):
    def test_refresh_waits_for_late_worker_registration(self):
        """worker 未注册时有界等待；等待期内注册成功则正常刷新。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = ClusterRepository(Path(tmp) / "cluster.sqlite3")
            app = FastAPI()
            app.include_router(devices_api.router, prefix="/api/cluster")

            class Svc:
                config = type("Cfg", (), {"local_worker_id": "local-A"})()
                repository = repo

            # 注册延迟 0.5s（模拟 LocalWorkerBridge 心跳延迟）
            timer = threading.Timer(
                0.5,
                lambda: repo.register_worker({
                    "worker_id": "local-A",
                    "name": "local",
                    "hostname": "h",
                    "address": "127.0.0.1",
                    "agent_version": "1",
                    "capabilities": {},
                }),
            )
            timer.start()
            self.addCleanup(timer.join)

            patches = [
                patch.object(devices_api, "service", lambda: Svc()),
                patch.object(cluster_api_mod, "_require_cluster_enabled",
                             return_value=None),
                patch.object(
                    cluster_api_mod, "_run_worker_command",
                    new=AsyncOnce(return_value={"devices": []})),
                patch.object(
                    devices_api, "require_authenticated_user_when_auth_required",
                    lambda request: CurrentUser(
                        id="u", username="u", role="admin")),
            ]
            for item in patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in patches])

            client = TestClient(app)
            response = client.post("/api/cluster/workers/local-A/refresh")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])

    def test_refresh_404_after_wait_window(self):
        """等待窗耗尽仍未注册则维持 404（远端 worker 不存在场景）。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = ClusterRepository(Path(tmp) / "cluster.sqlite3")
            app = FastAPI()
            app.include_router(devices_api.router, prefix="/api/cluster")

            class Svc:
                config = type("Cfg", (), {"local_worker_id": "local-A"})()
                repository = repo

            patches = [
                patch.object(devices_api, "service", lambda: Svc()),
                patch.object(cluster_api_mod, "_require_cluster_enabled",
                             return_value=None),
                patch.object(
                    devices_api, "require_authenticated_user_when_auth_required",
                    lambda request: CurrentUser(
                        id="u", username="u", role="admin")),
            ]
            for item in patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in patches])

            client = TestClient(app)
            response = client.post("/api/cluster/workers/ghost/refresh")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["detail"], "worker not found")


class AsyncOnce:
    """可 await 的一次性 mock（返回预定值）。"""

    def __init__(self, return_value):
        self._return_value = return_value

    async def __call__(self, *args, **kwargs):
        return self._return_value


if __name__ == "__main__":
    unittest.main()
