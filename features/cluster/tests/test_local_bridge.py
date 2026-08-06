"""Tests for the Local Worker Bridge that keeps the Controller registered
as ats-worker-controller inside the cluster database."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.cluster.config import ClusterConfig
from features.cluster.repository import ClusterRepository


class LocalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = ClusterRepository(Path(self.temp.name) / "cluster.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_bridge_registers_local_worker(self):
        from features.cluster.local_bridge import LocalWorkerBridge

        config = ClusterConfig(
            enabled=False,
            local_worker_id="ats-worker-controller",
        )
        bridge = LocalWorkerBridge(self.repo, config)
        bridge._register()
        worker = self.repo.get_worker("ats-worker-controller")
        self.assertIsNotNone(worker)
        self.assertEqual(worker["name"], "ats-worker-controller")
        self.assertEqual(worker["status"], "online")
        self.assertIn("adb", worker["capabilities"])

    def test_bridge_heartbeat_updates_devices_and_metrics(self):
        from features.cluster.local_bridge import LocalWorkerBridge

        config = ClusterConfig(enabled=False, local_worker_id="ats-worker-controller")
        bridge = LocalWorkerBridge(self.repo, config)
        bridge._register()
        with patch(
            "worker_agent.adb_proxy.recover_managed_state",
            return_value={"recovered": ["target"], "errors": []},
        ) as recover, patch(
            "features.devices.local_proxy_secret",
            return_value=b"x" * 32,
        ):
            bridge._heartbeat()
        worker = self.repo.get_worker("ats-worker-controller")
        self.assertIsNotNone(worker)
        self.assertGreaterEqual(worker["disk_free_gb"], 0)
        recover.assert_called_once_with(secret=b"x" * 32)

    def test_bridge_re_registers_after_offline(self):
        from features.cluster.local_bridge import LocalWorkerBridge

        config = ClusterConfig(enabled=False, local_worker_id="ats-worker-controller")
        bridge = LocalWorkerBridge(self.repo, config)
        bridge._register()
        self.repo.mark_worker_offline("ats-worker-controller")
        self.assertEqual(self.repo.get_worker("ats-worker-controller")["status"], "offline")
        bridge._registered = False
        bridge._register()
        self.assertEqual(self.repo.get_worker("ats-worker-controller")["status"], "online")


if __name__ == "__main__":
    unittest.main()
