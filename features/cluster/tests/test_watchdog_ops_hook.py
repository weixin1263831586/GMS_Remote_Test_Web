"""Watchdog incident-hook tests (phase 2): hook must never break monitoring."""

import unittest
from unittest import mock

from features.cluster.service import ClusterService


class WatchdogHookTests(unittest.TestCase):
    def test_list_workers_records_offline_transition(self):
        repository = mock.MagicMock()
        repository.list_workers.return_value = [
            {"id": "worker-a", "status": "online",
             "last_heartbeat_at": "2020-01-01T00:00:00Z"},
        ]
        repository.list_worker_tests.return_value = []
        service = ClusterService(repository, offline_seconds=45)
        with mock.patch(
            "foundation.ops_port.record_incident",
            return_value={"event": {}, "suppressed": False},
        ) as incident:
            service.list_workers()
        incident.assert_called_once()
        kwargs = incident.call_args.kwargs
        self.assertEqual(kwargs["kind"], "worker_offline")
        self.assertEqual(kwargs["subject"], "worker-a")
        repository.mark_worker_offline.assert_called_once_with("worker-a")

    def test_hook_failure_keeps_watchdog_alive(self):
        repository = mock.MagicMock()
        repository.list_workers.return_value = [
            {"id": "worker-a", "status": "online",
             "last_heartbeat_at": "2020-01-01T00:00:00Z"},
        ]
        repository.list_worker_tests.return_value = []
        service = ClusterService(repository, offline_seconds=45)
        with mock.patch(
            "foundation.ops_port.record_incident", side_effect=RuntimeError("boom")
        ):
            workers = service.list_workers()  # must not raise
        self.assertEqual(workers[0]["status"], "offline")

    def test_unconfigured_port_keeps_watchdog_quiet(self):
        repository = mock.MagicMock()
        repository.list_workers.return_value = [
            {"id": "worker-a", "status": "online",
             "last_heartbeat_at": "2020-01-01T00:00:00Z"},
        ]
        repository.list_worker_tests.return_value = []
        service = ClusterService(repository, offline_seconds=45)
        workers = service.list_workers()  # no ops registration at all
        self.assertEqual(workers[0]["status"], "offline")
        repository.mark_worker_offline.assert_called_once_with("worker-a")
