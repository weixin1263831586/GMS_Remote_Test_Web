import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.cluster import ClusterConfig, ClusterRepository, ClusterService
from features.cluster import api as cluster_api
from features.test_execution import execution_api
from features.test_execution.models import TestStartRequest as StartRequest


class TestDurableStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_start_rejects_external_busy_device(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClusterRepository(Path(directory) / "cluster.sqlite3")
            repository.register_worker({
                "worker_id": "ats-worker-controller", "agent_version": "controller-0.1.0",
                "max_jobs": 1, "name": "local", "hostname": "local",
                "address": "127.0.0.1", "capabilities": {},
            })
            repository.heartbeat("ats-worker-controller", {
                "running_jobs": [{"worker_job_id": "external-1", "job_id": "",
                                  "attempt_id": "", "status": "running",
                                  "devices": ["SERIAL-1"], "source": "external"}],
                "devices": [{"serial": "SERIAL-1", "state": "available"}],
            })
            service = ClusterService(
                repository, config=ClusterConfig(enabled=False)
            )
            previous = cluster_api.cluster_service
            cluster_api.cluster_service = service
            try:
                with (
                    patch.object(execution_api.runtime, 'generate_help_or_continue', return_value=None),
                    patch.object(execution_api.runtime, 'get_client_id_from_request', return_value='alice'),
                ):
                    result = await execution_api.start_test(
                        SimpleNamespace(),
                        req=StartRequest(worker_id="ats-worker-controller", devices=["SERIAL-1"]),
                    )
            finally:
                cluster_api.cluster_service = previous

        self.assertEqual(result.status_code, 409)

    async def test_local_worker_uses_durable_cluster_job_when_agent_is_online(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClusterRepository(Path(directory) / "cluster.sqlite3")
            repository.register_worker({
                "worker_id": "ats-worker-controller", "agent_version": "0.2.0", "max_jobs": 1,
                "name": "local", "hostname": "local", "address": "127.0.0.1",
                "capabilities": {},
            })
            previous = cluster_api.cluster_service
            cluster_api.cluster_service = ClusterService(repository)
            sentinel = {"success": True, "cluster_job_id": "job-1"}
            try:
                with (
                    patch.object(execution_api.runtime, 'generate_help_or_continue', return_value=None),
                    patch.object(execution_api.runtime, 'get_client_id_from_request', return_value='alice'),
                    patch.object(execution_api.runtime, 'start_cluster_test', return_value=sentinel) as start,
                ):
                    result = await execution_api.start_test(
                        SimpleNamespace(),
                        req=StartRequest(worker_id="ats-worker-controller", devices=["SERIAL-1"]),
                    )
            finally:
                cluster_api.cluster_service = previous

        self.assertEqual(result, sentinel)
        start.assert_called_once()

    async def test_configured_local_worker_id_is_not_dispatched_as_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClusterRepository(Path(directory) / "cluster.sqlite3")
            repository.register_worker({
                "worker_id": "controller-main", "agent_version": "0.2.0",
                "max_jobs": 1, "name": "local", "hostname": "local",
                "address": "127.0.0.1", "capabilities": {},
            })
            service = ClusterService(
                repository,
                config=ClusterConfig(enabled=True, local_worker_id="controller-main"),
            )
            previous = cluster_api.cluster_service
            cluster_api.cluster_service = service
            sentinel = {"success": True, "cluster_job_id": "job-local"}
            try:
                with (
                    patch.object(execution_api.runtime, "generate_help_or_continue", return_value=None),
                    patch.object(execution_api.runtime, "get_client_id_from_request", return_value="alice"),
                    patch.object(execution_api.runtime, "start_cluster_test", return_value=sentinel) as start,
                ):
                    result = await execution_api.start_test(
                        SimpleNamespace(),
                        req=StartRequest(worker_id="controller-main", devices=["SERIAL-1"]),
                    )
            finally:
                cluster_api.cluster_service = previous

        self.assertEqual(result, sentinel)
        self.assertEqual(start.call_args.args[0].worker_id, "controller-main")

    async def test_start_never_falls_back_to_process_local_task(self):
        with (
            patch.object(execution_api.runtime, 'generate_help_or_continue', return_value=None),
            patch.object(execution_api.runtime, 'get_client_id_from_request', return_value='alice'),
            patch('features.cluster.get_cluster_service', side_effect=RuntimeError('cluster unavailable')),
            patch.object(execution_api.runtime, 'start_cluster_test') as start,
        ):
            result = await execution_api.start_test(
                SimpleNamespace(),
                req=StartRequest(test_type='cts', devices=['SERIAL-1']),
            )

        self.assertEqual(result.status_code, 503)
        start.assert_not_called()

    async def test_local_worker_offline_returns_service_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClusterRepository(Path(directory) / "cluster.sqlite3")
            repository.register_worker({
                "worker_id": "ats-worker-controller", "agent_version": "controller-0.1.0",
                "max_jobs": 1, "name": "local", "hostname": "local",
                "address": "127.0.0.1", "capabilities": {},
            })
            previous = cluster_api.cluster_service
            cluster_api.cluster_service = ClusterService(repository)
            try:
                with (
                    patch.object(execution_api.runtime, "generate_help_or_continue", return_value=None),
                    patch.object(execution_api.runtime, "get_client_id_from_request", return_value="alice"),
                    patch.object(execution_api.runtime, "start_cluster_test") as start,
                ):
                    result = await execution_api.start_test(
                        SimpleNamespace(),
                        req=StartRequest(devices=["SERIAL-1"]),
                    )
            finally:
                cluster_api.cluster_service = previous

        self.assertEqual(result.status_code, 503)
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
