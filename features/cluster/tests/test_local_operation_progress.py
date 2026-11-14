"""本机 durable_local ``aoa_sequence`` 逐条进度上报（execute_local_operation）。

远端 Worker 路径经 HTTP 上报 command events；本机路径必须直接写
``repository.append_command_events``，否则浏览器回放高亮对本机设备失效。
"""
import unittest

from features.cluster.device_action_operations import execute_local_operation


class _FakeClaims:
    def active_claim(self, device_id):
        return {"id": "lease-1", "generation": 3}


class _FakeRepository:
    def __init__(self, fail_writes: bool = False):
        self.claims = _FakeClaims()
        self.fail_writes = fail_writes
        self.events: list[dict] = []
        self.acked: list[tuple] = []

    def ack_command(self, worker_id, command_id, data):
        self.acked.append((command_id, data["status"]))

    def release(self, source_id, status="completed"):
        pass

    def append_command_events(self, worker_id, command_id, events):
        if self.fail_writes:
            raise RuntimeError("sqlite busy")
        self.events.extend(events)
        return len(events)


def _aoa_command() -> dict:
    return {
        "id": "cmd-local-aoa",
        "worker_id": "ats-worker-controller",
        "payload": {
            "action": "aoa_sequence",
            "devices": ["ats-worker-controller:d1"],
            "lease_tokens": [{"device_id": "ats-worker-controller:d1",
                              "lease_id": "lease-1", "generation": 3}],
        },
    }


class LocalOperationProgressTests(unittest.TestCase):
    def test_aoa_sequence_receives_callback_and_writes_events(self):
        repo = _FakeRepository()
        seen: dict = {}

        def execute(action, devices, payload, progress_callback=None, progress_event_callback=None):
            seen["callback"] = progress_callback
            self.assertIsNotNone(progress_callback)
            progress_callback(1, 3, "click 180 527")
            progress_callback(2, 3, "sleep 2000")
            return {"success": True}

        execute_local_operation(repo, _aoa_command(), execute)
        self.assertTrue(seen["callback"])
        self.assertEqual([item["sequence"] for item in repo.events], [1, 2])
        first = repo.events[0]
        self.assertEqual(first["event_type"], "progress")
        self.assertEqual(first["source"], "aoa")
        self.assertEqual(first["payload"]["index"], 1)
        self.assertEqual(first["payload"]["total"], 3)
        self.assertEqual(first["payload"]["command"], "click 180 527")

    def test_progress_write_failure_does_not_break_playback(self):
        repo = _FakeRepository(fail_writes=True)

        def execute(action, devices, payload, progress_callback=None, progress_event_callback=None):
            progress_callback(1, 2, "click 180 527")
            return {"success": True}

        execute_local_operation(repo, _aoa_command(), execute)
        self.assertEqual(len(repo.events), 0)  # 上报失败被吞，回放照常

    def test_non_aoa_action_gets_no_callback(self):
        repo = _FakeRepository()
        command = _aoa_command()
        command["payload"]["action"] = "screenshot"

        def execute(action, devices, payload, progress_callback=None, progress_event_callback=None):
            self.assertIsNone(progress_callback)
            return {"success": True}

        execute_local_operation(repo, command, execute)
