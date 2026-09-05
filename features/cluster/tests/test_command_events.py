"""Worker 命令实时日志通道（command events）单元测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.cluster import commands_api, repository_schema
from features.cluster.repository import ClusterRepository


def _repository(tmp: str) -> ClusterRepository:
    repo = ClusterRepository(Path(tmp) / "cluster.sqlite3")
    with repo.connect() as conn:
        conn.execute("""INSERT INTO cluster_workers
            (id,name,hostname,address,agent_version,status,capabilities_json,
             registered_at,last_heartbeat_at,updated_at)
            VALUES('worker-A','A','h','a','v','online','{}',
                   '2026-01-01','2026-01-01','2026-01-01')""")
        conn.execute("""INSERT INTO cluster_commands
            (id,worker_id,command_type,job_id,attempt_id,dispatch_token,
             payload_json,status,result_json,error,created_at,updated_at,
             delivered_at,acknowledged_at)
            VALUES('cmd-1','worker-A','flash_gsi','','',
                   'tok','{}','running','{}','','2026-01-01','2026-01-01',
                   '','')""")
    return repo


class CommandEventsRepositoryTests(unittest.TestCase):
    def test_append_and_list_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            inserted = repo.append_command_events("worker-A", "cmd-1", [
                {"sequence": 0, "message": "fastboot flash system"},
                {"sequence": 1, "message": "OKAY", "level": "info"},
            ])
            self.assertEqual(inserted, 2)
            events = repo.list_command_events("cmd-1", after=-1)
            self.assertEqual([e["message"] for e in events],
                             ["fastboot flash system", "OKAY"])
            self.assertEqual([e["sequence"] for e in events], [0, 1])

    def test_sequence_dedup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            repo.append_command_events("worker-A", "cmd-1", [
                {"sequence": 0, "message": "line"}])
            # Worker 重发同一批（重试）：UNIQUE 约束去重，不重复入库。
            inserted = repo.append_command_events("worker-A", "cmd-1", [
                {"sequence": 0, "message": "line"}])
            self.assertEqual(inserted, 0)
            self.assertEqual(len(repo.list_command_events("cmd-1")), 1)

    def test_wrong_worker_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            inserted = repo.append_command_events("worker-B", "cmd-1", [
                {"sequence": 0, "message": "spoof"}])
            self.assertEqual(inserted, 0)
            self.assertEqual(repo.list_command_events("cmd-1"), [])

    def test_after_cursor_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            repo.append_command_events("worker-A", "cmd-1", [
                {"sequence": i, "message": f"line-{i}"} for i in range(5)])
            events = repo.list_command_events("cmd-1", after=2)
            self.assertEqual([e["sequence"] for e in events], [3, 4])


class CommandEventsApiTests(unittest.TestCase):
    def _client(self, repo, patches):
        """构造 TestClient 并注册 patch 清理。

        注意这里必须是普通方法而不是生成器：调用方 ``next(self._client(...))``
        会立刻丢弃生成器引用，GC 触发 GeneratorExit 时 finally 会把刚启动的
        patch 全部 stop 掉，端点请求时 service 已被还原成未配置状态。
        """
        app = FastAPI()
        app.include_router(commands_api.router, prefix="/api/cluster")
        holder = mock.Mock()
        holder.return_value.repository = repo
        started = [mock.patch.object(commands_api, "service", lambda: holder())]
        started.extend(patches)
        for item in started:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in started])
        return TestClient(app)

    def test_worker_events_endpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            patches = [
                mock.patch.object(commands_api, "_authenticate", return_value=None),
                mock.patch.object(commands_api, "_require_worker_session", return_value=None),
            ]
            client = self._client(repo, patches)
            response = client.post(
                "/api/cluster/workers/worker-A/commands/cmd-1/events",
                json={"events": [{"sequence": 0, "message": "hello"}]},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            events = repo.list_command_events("cmd-1")
            self.assertEqual(events[0]["message"], "hello")

    def test_command_events_rejects_wrong_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            patches = [
                mock.patch.object(commands_api, "_authenticate", return_value=None),
                mock.patch.object(commands_api, "_require_worker_session", return_value=None),
            ]
            client = self._client(repo, patches)
            response = client.post(
                "/api/cluster/workers/worker-OTHER/commands/cmd-1/events",
                json={"events": [{"sequence": 0, "message": "spoof"}]},
            )
            # Repository 层按 worker_id 归属校验，伪造 Worker 不会入库。
            self.assertEqual(repo.list_command_events("cmd-1"), [])

    def test_command_events_repository_level(self):
        """repository 层幂等/归属校验已由上面用例覆盖；这里验证 GET 端点集成。

        GET /commands/{id}/events 需要 _require_command_access（走真实 auth），
        单元层以 e2e 覆盖；此处直接验证响应结构（mock 掉权限）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            repo.append_command_events("worker-A", "cmd-1", [
                {"sequence": 0, "message": "line-0"},
                {"sequence": 1, "message": "line-1"},
            ])
            patches = [
                mock.patch.object(commands_api, "_require_command_access", return_value=None),
            ]
            client = self._client(repo, patches)
            response = client.get("/api/cluster/commands/cmd-1/events?after=0")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["success"])
            self.assertEqual([e["sequence"] for e in data["events"]], [1])
            self.assertEqual(data["command"], {"id": "cmd-1", "status": "running"})


if __name__ == "__main__":
    unittest.main()
