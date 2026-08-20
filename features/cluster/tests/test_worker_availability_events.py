"""Worker 在线/离线翻转事件广播测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.cluster.repository import ClusterRepository
from foundation.events import (
    EVENT_WORKER_AVAILABILITY_CHANGED,
    event_bus,
)


class WorkerAvailabilityEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = ClusterRepository(Path(self.temp.name) / "cluster.sqlite3")
        self.events = []
        event_bus.subscribe(
            EVENT_WORKER_AVAILABILITY_CHANGED,
            lambda _event_type, payload: self.events.append(dict(payload)),
        )

    def tearDown(self):
        self.temp.cleanup()

    def register(self):
        return self.repo.register_worker({
            "worker_id": "worker-246", "name": "remote", "hostname": "ats-246",
            "address": "172.16.14.246", "agent_version": "1", "max_jobs": 1,
            "capabilities": {"adb": True},
        })

    def heartbeat(self):
        return self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [], "suites": [],
        })

    def test_offline_flip_emits_warning_event(self):
        self.register()
        self.assertEqual(self.events, [])
        self.repo.mark_worker_offline("worker-246")
        self.assertEqual(self.events, [{
            "worker_id": "worker-246", "name": "remote", "status": "offline",
        }])

    def test_repeated_offline_marks_do_not_duplicate_events(self):
        self.register()
        self.repo.mark_worker_offline("worker-246")
        self.repo.mark_worker_offline("worker-246")
        self.assertEqual(len(self.events), 1)

    def test_offline_worker_reconnect_emits_online_event(self):
        self.register()
        self.repo.mark_worker_offline("worker-246")
        self.events.clear()
        self.register()
        self.assertEqual(self.events, [{
            "worker_id": "worker-246", "name": "remote", "status": "online",
        }])

    def test_heartbeat_recovery_without_reregister_emits_online_event(self):
        # 会话保持不变时（进程内重启的 Agent 复用会话），心跳直接把
        # offline 状态翻转为 online，也必须广播上线事件。
        self.register()
        self.repo.mark_worker_offline("worker-246")
        self.events.clear()
        self.heartbeat()
        self.assertEqual(self.events, [{
            "worker_id": "worker-246", "name": "remote", "status": "online",
        }])

    def test_steady_state_heartbeats_stay_silent(self):
        self.register()
        self.heartbeat()
        self.heartbeat()
        self.assertEqual(self.events, [])

    def test_initial_registration_stays_silent(self):
        self.register()
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
