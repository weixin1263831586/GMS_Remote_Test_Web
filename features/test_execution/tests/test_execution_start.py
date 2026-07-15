import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.cluster import ClusterConfig, ClusterRepository, ClusterService
from features.cluster import api as cluster_api
from features.test_execution import execution_api
from features.test_execution.models import TestStartRequest as StartRequest


class TestStartConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_legacy_fallback_rejects_external_busy_device(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClusterRepository(Path(directory) / "cluster.sqlite3")
            repository.register_worker({
                "worker_id": "worker-local", "agent_version": "controller-0.1.0",
                "max_jobs": 1, "name": "local", "hostname": "local",
                "address": "127.0.0.1", "capabilities": {},
            })
            repository.heartbeat("worker-local", {
                "running_jobs": [{"worker_job_id": "external-1", "job_id": "",
                                  "attempt_id": "", "status": "running",
                                  "devices": ["SERIAL-1"], "source": "external"}],
                "devices": [{"serial": "SERIAL-1", "state": "available"}],
            })
            service = ClusterService(repository)
            service.set_runtime_enabled(False)
            previous = cluster_api.cluster_service
            cluster_api.cluster_service = service
            try:
                with (
                    patch.object(execution_api.runtime, 'generate_help_or_continue', return_value=None),
                    patch.object(execution_api.runtime, 'get_client_id_from_request', return_value='alice'),
                    patch.object(execution_api.runtime, 'acquire_test_devices') as acquire,
                ):
                    result = await execution_api.start_test(
                        SimpleNamespace(),
                        req=StartRequest(worker_id="worker-local", devices=["SERIAL-1"]),
                    )
            finally:
                cluster_api.cluster_service = previous

        self.assertEqual(result.status_code, 409)
        acquire.assert_not_called()

    async def test_local_worker_uses_durable_cluster_job_when_agent_is_online(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClusterRepository(Path(directory) / "cluster.sqlite3")
            repository.register_worker({
                "worker_id": "worker-local", "agent_version": "0.2.0", "max_jobs": 1,
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
                    patch.object(execution_api.runtime, 'acquire_test_devices') as acquire,
                ):
                    result = await execution_api.start_test(
                        SimpleNamespace(),
                        req=StartRequest(worker_id="worker-local", devices=["SERIAL-1"]),
                    )
            finally:
                cluster_api.cluster_service = previous

        self.assertEqual(result, sentinel)
        start.assert_called_once()
        acquire.assert_not_called()

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
                    patch.object(execution_api.runtime, "acquire_test_devices") as acquire,
                ):
                    result = await execution_api.start_test(
                        SimpleNamespace(),
                        req=StartRequest(worker_id="controller-main", devices=["SERIAL-1"]),
                    )
            finally:
                cluster_api.cluster_service = previous

        self.assertEqual(result, sentinel)
        self.assertEqual(start.call_args.args[0].worker_id, "controller-main")
        acquire.assert_not_called()

    async def test_concurrent_start_for_same_client_is_rejected(self):
        state = {}
        acquisition_started = asyncio.Event()
        allow_acquisition = asyncio.Event()

        async def acquire(**_kwargs):
            acquisition_started.set()
            await allow_acquisition.wait()
            return ['SERIAL-1'], []

        async def background(*_args):
            return None

        global_state = SimpleNamespace(
            user_states_lock=threading.RLock(),
            background_tasks=set(),
        )
        config_manager = SimpleNamespace(load_config=lambda: {})
        request = SimpleNamespace()
        req = StartRequest(test_type='cts', devices=['SERIAL-1'])

        with (
            patch.object(execution_api.runtime, 'global_state', global_state),
            patch.object(execution_api.runtime, 'config_manager', config_manager),
            patch.object(execution_api.runtime, 'generate_help_or_continue', return_value=None),
            patch.object(execution_api.runtime, 'get_client_id_from_request', return_value='alice'),
            patch.object(execution_api.runtime, 'acquire_test_devices', side_effect=acquire),
            patch('features.cluster.get_cluster_service', side_effect=RuntimeError('cluster unavailable')),
            patch.object(execution_api, 'get_client_username_from_request', return_value='alice'),
            patch.object(execution_api, 'get_client_display_id_from_request', return_value='alice'),
            patch.object(execution_api, 'get_or_create_user_state', return_value=state),
            patch.object(execution_api, 'update_user_state_field', side_effect=lambda _id, data: state.update(data)),
            patch.object(execution_api, '_run_test_background', side_effect=background),
        ):
            first_task = asyncio.create_task(
                execution_api.start_test(request, req=req)
            )
            await acquisition_started.wait()
            second = await execution_api.start_test(request, req=req)
            allow_acquisition.set()
            first = await first_task
            await asyncio.sleep(0)

        self.assertEqual(second.status_code, 400)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(state['starting'])
        self.assertTrue(state['running'])


if __name__ == '__main__':
    unittest.main()
