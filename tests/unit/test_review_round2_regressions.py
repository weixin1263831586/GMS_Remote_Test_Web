"""第二轮评审回归测试：device identity / uploader ordering / claim borrow。

覆盖 4.txt 第二轮评审要求的核心边界用例：

- 含 ":" 的 serial（ADB TCP / ADB Proxy / 跨 Worker 前缀）在
  startTest 校验与 workspace normalize 中不被误判为 Worker 前缀；
- CommandEventUploader 失败 batch 原地重试、flush 等待 in-flight ACK；
- devices.use_leased：普通 user 借用自己已有 claim 不再自冲突；
- DeviceActionSpec 覆盖与模型 action 集合一致。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from features.cluster.models import ClusterDeviceAction, WorkerRegistration
from features.cluster.repository import ClusterRepository
from worker_agent.command_events import CommandEventUploader


def _repository(tmp: str) -> ClusterRepository:
    repo = ClusterRepository(Path(tmp) / "cluster.sqlite3")
    repo.register_worker(WorkerRegistration(
        worker_id="worker-A", name="A", hostname="a", address="10.0.0.1",
        agent_version="1", session_id="s1").model_dump())
    repo.refresh_worker_devices("worker-A", [
        {"serial": "RK3576GMS1", "transport": "local_usb",
         "state": "available", "properties": {}},
        {"serial": "localhost:40001", "transport": "adb_proxy",
         "state": "available", "properties": {}},
        {"serial": "192.168.1.20:5555", "transport": "local_usb",
         "state": "available", "properties": {}},
    ])
    return repo


class DeviceOwnershipResolutionTests(unittest.TestCase):
    """worker:serial 只能通过已知 Worker ID + inventory canonical ID 识别。"""

    def test_start_local_adb_proxy_serial_with_colon(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            self.assertIsNotNone(
                repo.resolve_worker_device("worker-A", "localhost:40001"))
            self.assertIsNotNone(
                repo.resolve_worker_device("worker-A", "worker-A:localhost:40001"))

    def test_start_tcp_adb_serial_with_colon(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            self.assertIsNotNone(
                repo.resolve_worker_device("worker-A", "192.168.1.20:5555"))

    def test_cross_worker_prefixed_device_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            self.assertIsNone(
                repo.resolve_worker_device("worker-A", "worker-B:RK3576GMS1"))

    def test_unknown_serial_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            self.assertIsNone(
                repo.resolve_worker_device("worker-A", "GHOST-Serial"))


class CommandEventUploaderOrderingTests(unittest.TestCase):
    def test_command_event_retry_preserves_sequence_order(self):
        uploaded: list[int] = []
        attempts = {"count": 0}

        def upload(batch):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("controller down")
            uploaded.extend(event["sequence"] for event in batch)

        uploader = CommandEventUploader(upload, batch_size=5)
        for i in range(10):
            uploader.submit({"sequence": i})
        deadline = time.monotonic() + 10
        while len(uploaded) < 10 and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(uploaded, list(range(10)))

    def test_command_event_flush_waits_for_inflight(self):
        uploader = CommandEventUploader(
            lambda batch: time.sleep(0.8), batch_size=5)
        for i in range(5):
            uploader.submit({"sequence": i})
        time.sleep(0.3)  # uploader 已 dequeue，HTTP 仍 in-flight
        started = time.monotonic()
        uploader.flush(timeout=6)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)


class UseLeasedBorrowTests(unittest.TestCase):
    """普通 user 借用自己已有 claim，不再被 acquire 自冲突 409。"""

    def test_plain_user_can_mutate_own_reserved_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            repo.reserve_devices(
                "worker-A", ["RK3576GMS1"], owner_id="alice", source_id="run-1")
            # 旧路径：owner 相同但 source 不同 → 冲突
            with self.assertRaises(ValueError):
                repo.acquire_device_operation_claim(
                    "worker-A", ["RK3576GMS1"], owner_id="alice",
                    source_type="cluster-device-action",
                    source_id="operation:new", ttl_seconds=3600,
                    username="alice")
            # borrow 路径：已有 claim 即 owned
            owned = {
                str(claim.get("device_key") or "")
                for claim in repo.claims.list_active(owner_id="alice")
            }
            owned |= repo.owned_reservation_device_ids("alice")
            self.assertIn("worker-A:RK3576GMS1", owned)

    def test_plain_user_cannot_mutate_other_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repository(tmp)
            repo.reserve_devices(
                "worker-A", ["RK3576GMS1"], owner_id="bob", source_id="run-1")
            owned = {
                str(claim.get("device_key") or "")
                for claim in repo.claims.list_active(owner_id="alice")
            }
            owned |= repo.owned_reservation_device_ids("alice")
            self.assertNotIn("worker-A:RK3576GMS1", owned)


class DeviceActionSpecCoverageTests(unittest.TestCase):
    def test_device_action_specs_cover_model_action_enum(self):
        from features.cluster.device_action_spec import DEVICE_ACTION_SPECS

        model_actions = {
            member.value
            for member in (
                ClusterDeviceAction.model_fields["action"].annotation.__members__.values()
                if hasattr(
                    ClusterDeviceAction.model_fields["action"].annotation,
                    "__members__",
                )
                else []
            )
        }
        self.assertTrue(model_actions, "action field must be a DeviceAction enum")
        self.assertEqual(
            model_actions,
            set(DEVICE_ACTION_SPECS),
            "DeviceActionSpec 与 ClusterDeviceAction.action 枚举漂移",
        )


if __name__ == "__main__":
    unittest.main()
