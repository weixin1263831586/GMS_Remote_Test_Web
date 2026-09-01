"""Tests for remote Worker USB/IP assignment verification scheduling."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import foundation.cluster_port as cluster_port
from features.devices import usbip_status_reconcile


def _assignment(
    worker_id="ats-worker-246",
    busid="1-8",
    status="unknown",
    generation=3,
    source_host="172.16.14.66",
    device_host="hcq@172.16.14.66",
    serials=None,
):
    return {
        "device_host": device_host,
        "source_host": source_host,
        "worker_id": worker_id,
        "busid": busid,
        "device_serials": serials or ["rk3572test"],
        "status": status,
        "generation": generation,
        "timestamp": 1,
    }


class _FakeCluster:
    def __init__(self, worker_status="online"):
        self.repository = SimpleNamespace(
            get_worker=lambda worker_id: {"status": worker_status},
            refresh_worker_devices=lambda worker_id, devices: None,
        )


class RemoteVerifySchedulingTests(unittest.TestCase):
    def setUp(self):
        usbip_status_reconcile._verify_throttle.clear()
        usbip_status_reconcile._running_verify_tasks.clear()

    def tearDown(self):
        for task in list(usbip_status_reconcile._running_verify_tasks):
            task.cancel()

    def test_pending_targets_only_match_remote_unknown(self):
        local = _assignment(worker_id="ats-worker-controller")
        remote_unknown = _assignment()
        remote_attached = _assignment(status="attached")

        with patch(
            "features.devices.integrations_api._local_worker_id",
            return_value="ats-worker-controller",
        ):
            self.assertTrue(usbip_status_reconcile.pending_remote_verify_targets(
                [local, remote_unknown, remote_attached],
            ))
            self.assertFalse(usbip_status_reconcile.pending_remote_verify_targets(
                [local, remote_attached],
            ))

    def test_schedule_skips_when_no_remote_unknown(self):
        fired = []

        async def fake_verify(device_host, assignments):
            fired.append(device_host)

        async def runner():
            usbip_status_reconcile.schedule_remote_usbip_verify(
                "hcq@172.16.14.66",
                [_assignment(status="attached")],
            )
            await asyncio.gather(
                *usbip_status_reconcile._running_verify_tasks,
                return_exceptions=True,
            )

        with patch.object(
            usbip_status_reconcile,
            "verify_remote_usbip_assignments",
            fake_verify,
        ):
            asyncio.run(runner())

        self.assertEqual(fired, [])

    def test_schedule_triggers_remote_verify(self):
        fired = []

        async def fake_verify(device_host, assignments):
            fired.append((device_host, len(assignments)))

        async def runner():
            usbip_status_reconcile.schedule_remote_usbip_verify(
                "hcq@172.16.14.66",
                [_assignment()],
            )
            await asyncio.gather(
                *usbip_status_reconcile._running_verify_tasks,
                return_exceptions=True,
            )

        with patch.object(
            usbip_status_reconcile,
            "verify_remote_usbip_assignments",
            fake_verify,
        ):
            asyncio.run(runner())

        self.assertEqual(fired, [("hcq@172.16.14.66", 1)])


class RemoteVerifyExecutionTests(unittest.TestCase):
    def setUp(self):
        usbip_status_reconcile._verify_throttle.clear()
        usbip_status_reconcile._running_verify_tasks.clear()
        self.runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-8": _assignment(),
            },
        }
    def tearDown(self):
        for task in list(usbip_status_reconcile._running_verify_tasks):
            task.cancel()

    def _config_manager(self):
        runtime_config = self.runtime_config

        class FakeConfigManager:
            def get_runtime_config(self):
                return runtime_config

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        return FakeConfigManager()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_unknown_remote_assignment_upgrades_after_idempotent_attach(self):
        commands = []
        refreshed = []

        async def fake_run_worker(worker_id, kind, payload, timeout=None):
            commands.append((worker_id, kind, payload))
            return {
                "attached_busids": list(payload["busids"]),
                "already_attached_busids": list(payload["busids"]),
                "devices": [{"serial": "rk3572test", "state": "available"}],
            }

        fake_cluster = _FakeCluster()
        fake_cluster.repository.refresh_worker_devices = (
            lambda worker_id, devices: refreshed.append((worker_id, devices))
        )

        with patch(
            "features.devices.integrations_api._local_worker_id",
            return_value="ats-worker-controller",
        ), patch(
            "features.devices.runtime.config_manager", self._config_manager(),
        ), patch.object(
            cluster_port, "get_cluster_service", lambda: fake_cluster,
        ), patch.object(
            cluster_port, "run_worker_command", fake_run_worker,
        ):
            self._run(usbip_status_reconcile.verify_remote_usbip_assignments(
                "hcq@172.16.14.66",
                [_assignment()],
            ))

        self.assertEqual(len(commands), 1)
        worker_id, kind, payload = commands[0]
        self.assertEqual(worker_id, "ats-worker-246")
        self.assertEqual(kind, "usbip_attach")
        self.assertEqual(payload["busids"], ["1-8"])
        self.assertEqual(payload["source_host"], "172.16.14.66")
        self.assertEqual(payload["generation"], 3)

        upgraded = self.runtime_config["usbip_cluster_assignments"][
            "hcq@172.16.14.66|1-8"
        ]
        self.assertEqual(upgraded["status"], "attached")
        self.assertEqual(refreshed[0][0], "ats-worker-246")
        self.assertEqual(refreshed[0][1][0]["serial"], "rk3572test")

    def test_worker_command_failure_keeps_unknown(self):
        async def fake_run_worker(worker_id, kind, payload, timeout=None):
            raise RuntimeError("worker unreachable")

        with patch(
            "features.devices.integrations_api._local_worker_id",
            return_value="ats-worker-controller",
        ), patch(
            "features.devices.runtime.config_manager", self._config_manager(),
        ), patch.object(
            cluster_port, "get_cluster_service", lambda: _FakeCluster(),
        ), patch.object(
            cluster_port, "run_worker_command", fake_run_worker,
        ):
            self._run(usbip_status_reconcile.verify_remote_usbip_assignments(
                "hcq@172.16.14.66",
                [_assignment()],
            ))

        kept = self.runtime_config["usbip_cluster_assignments"][
            "hcq@172.16.14.66|1-8"
        ]
        self.assertEqual(kept["status"], "unknown")

    def test_offline_worker_is_skipped(self):
        async def fake_run_worker(*args, **kwargs):
            raise AssertionError("must not run for offline worker")

        with patch(
            "features.devices.integrations_api._local_worker_id",
            return_value="ats-worker-controller",
        ), patch(
            "features.devices.runtime.config_manager", self._config_manager(),
        ), patch.object(
            cluster_port, "get_cluster_service",
            lambda: _FakeCluster(worker_status="offline"),
        ), patch.object(
            cluster_port, "run_worker_command", fake_run_worker,
        ):
            self._run(usbip_status_reconcile.verify_remote_usbip_assignments(
                "hcq@172.16.14.66",
                [_assignment()],
            ))

        self.assertEqual(
            self.runtime_config["usbip_cluster_assignments"][
                "hcq@172.16.14.66|1-8"
            ]["status"],
            "unknown",
        )

    def test_verify_throttles_repeated_status_polling(self):
        commands = []

        async def fake_run_worker(worker_id, kind, payload, timeout=None):
            commands.append(payload)
            raise RuntimeError("worker unreachable")

        fake_cluster = _FakeCluster()

        async def run_once():
            await usbip_status_reconcile.verify_remote_usbip_assignments(
                "hcq@172.16.14.66",
                [_assignment()],
            )

        with patch(
            "features.devices.integrations_api._local_worker_id",
            return_value="ats-worker-controller",
        ), patch(
            "features.devices.runtime.config_manager", self._config_manager(),
        ), patch.object(
            cluster_port, "get_cluster_service", lambda: fake_cluster,
        ), patch.object(
            cluster_port, "run_worker_command", fake_run_worker,
        ):
            # 失败后分配保持 unknown；紧随的状态轮询再次触发核对时，
            # 节流窗口必须挡住第二次下发。
            self._run(run_once())
            self.assertEqual(
                self.runtime_config["usbip_cluster_assignments"][
                    "hcq@172.16.14.66|1-8"
                ]["status"],
                "unknown",
            )
            self._run(run_once())

        self.assertEqual(len(commands), 1)


if __name__ == "__main__":
    unittest.main()
