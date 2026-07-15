"""Tests for the background automation worker and device selector.

The worker functions accept injected automation/build services so tests do not
touch the module-level singletons.
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeLockManager:
    def __init__(self, locked: set[str] | None = None):
        self._locked = locked or set()

    def get_all_locks(self):
        return {serial: {} for serial in self._locked}


class _FakeDeviceManager:
    def __init__(self, serials, info=None):
        self._serials = serials
        self._info = info or {}

    def get_connected_devices(self):
        return list(self._serials)

    def get_device_info(self, serial):
        return self._info.get(serial, {})


class _FakeBuildService:
    """Minimal build service stub exercising the worker code paths."""

    def __init__(self, store, running_jobs=None):
        self.store = store
        self.polled = []
        # Optional pre-seeded job dicts returned by list_jobs(status="running")
        self._running_override = running_jobs

    def list_jobs(self, status="", limit=50):
        if status == "running" and self._running_override is not None:
            return list(self._running_override)
        return self.store.list_jobs(status=status, limit=limit)

    def poll_job(self, job_id):
        self.polled.append(job_id)
        return self.store.get_job(job_id)

    def start_queued_jobs(self):
        return 0


class _FakeAutomationService:
    def __init__(self, store, executor_name="http"):
        self.store = store
        self.ticks = 0
        self._executor_name = executor_name

    def list_runs(self, status="", limit=50):
        return self.store.list_runs(status=status, limit=limit)

    def orchestrator(self, executor_name="stub"):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.orchestrator import AutomationOrchestrator

        return AutomationOrchestrator(self.store, StubAutomationExecutor())

    def list_events(self, run_id):
        return self.store.list_events(run_id)

    def worker_tick(self, executor_name="stub"):
        # Advance exactly one run per tick, like the real advance_next.
        orch = self.orchestrator(self._executor_name)
        result = orch.advance_next()
        self.ticks += 1
        return result


class _make:
    """Helpers to build build-job and automation-run rows for tests."""


def _make_build_job(store, *, job_id, status, started_at, created_at=None):
    return store.create_job({
        "id": job_id,
        "server_id": "srv",
        "template_id": "tpl",
        "source_type": "test",
        "source_key": f"key:{job_id}",
        "owner": "tester",
        "automation_run_id": "",
        "status": status,
        "remote_session": "",
        "remote_workspace": "/ws",
        "remote_log_path": "",
        "command": "./build.sh",
        "parameters_json": "{}",
        "artifact_json": "[]",
        "error": "",
        "created_at": created_at or started_at,
        "updated_at": started_at,
        "started_at": started_at,
        "finished_at": "",
    })


class StaleSweepTests(unittest.TestCase):
    def test_marks_stale_running_build_and_cancels_old_run(self):
        from features.automation.models import RUN_STATUS_CANCELLED, AutomationRunCreateRequest
        from features.automation.repository import AutomationStore
        from features.automation.worker import stale_sweep_once
        from features.build import JOB_FAILED, BuildStore

        cfg = {
            "stale_build_seconds": 7200,
            "stale_run_seconds": 86400,
            "waiting_device_timeout_seconds": 1800,
            "executor": "stub",
        }
        long_ago = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))

        with TemporaryDirectory() as tmp:
            build_store = BuildStore(Path(tmp) / "build.sqlite3")
            _make_build_job(build_store, job_id="j_old", status="running", started_at=long_ago)
            _make_build_job(build_store, job_id="j_new", status="running", started_at=recent)

            auto_store = AutomationStore(Path(tmp) / "automation.sqlite3")
            auto_store.create_run(AutomationRunCreateRequest(
                profile_id="p", artifact_path="/tmp/x", devices=["S1"],
                test_plan={"test_type": "CTS"},
            ).to_run_dict("r_old"))
            # update_run forces updated_at=now, so backdate via raw SQL.
            with auto_store._connect() as conn:
                conn.execute(
                    "UPDATE automation_runs SET updated_at = ? WHERE id = ?",
                    (long_ago, "r_old"),
                )

            build_svc = _FakeBuildService(build_store)
            auto_svc = _FakeAutomationService(auto_store)
            real_orchestrator = auto_svc.orchestrator()
            real_orchestrator.cancel_run = MagicMock(
                wraps=real_orchestrator.cancel_run
            )
            auto_svc.orchestrator = MagicMock(return_value=real_orchestrator)

            result = stale_sweep_once(cfg, automation_service=auto_svc, build_service=build_svc)

            self.assertEqual(result["builds_marked"], 1)
            self.assertEqual(build_store.get_job("j_old")["status"], JOB_FAILED)
            self.assertEqual(build_store.get_job("j_new")["status"], "running")
            self.assertEqual(auto_store.get_run("r_old")["status"], RUN_STATUS_CANCELLED)
            real_orchestrator.cancel_run.assert_called_once_with(
                "r_old", reason="stale before worker start", cleanup=True
            )


class PollRunningBuildsTests(unittest.TestCase):
    def test_polls_each_running_job(self):
        from features.automation.worker import poll_running_builds_sync
        from features.build import BuildStore

        with TemporaryDirectory() as tmp:
            store = BuildStore(Path(tmp) / "build.sqlite3")
            now = _iso(datetime.now(timezone.utc))
            _make_build_job(store, job_id="j1", status="running", started_at=now)
            _make_build_job(store, job_id="j2", status="running", started_at=now)
            svc = _FakeBuildService(store)

            count = poll_running_builds_sync(svc)

            self.assertEqual(count, 2)
            self.assertEqual(sorted(svc.polled), ["j1", "j2"])


class WaitingDeviceTimeoutTests(unittest.TestCase):
    def test_cancels_timed_out_waiting_run_only(self):
        from features.automation.models import (
            RUN_STATUS_CANCELLED,
            RUN_STATUS_WAITING_DEVICE,
            AutomationRunCreateRequest,
        )
        from features.automation.repository import AutomationStore
        from features.automation.worker import sweep_waiting_device_timeouts

        cfg = {"waiting_device_timeout_seconds": 1800, "executor": "stub"}
        long_ago = _iso(datetime.now(timezone.utc) - timedelta(hours=2))

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest(
                profile_id="p", artifact_path="/tmp/x", devices=["S1"],
                test_plan={"test_type": "CTS"},
            ).to_run_dict("r_old"))
            store.update_run("r_old", status=RUN_STATUS_WAITING_DEVICE)
            with store._connect() as conn:
                conn.execute(
                    "UPDATE automation_runs SET updated_at = ? WHERE id = ?",
                    (long_ago, "r_old"),
                )

            store.create_run(AutomationRunCreateRequest(
                profile_id="p", artifact_path="/tmp/x", devices=["S2"],
                test_plan={"test_type": "CTS"},
            ).to_run_dict("r_new"))
            store.update_run("r_new", status=RUN_STATUS_WAITING_DEVICE)

            svc = _FakeAutomationService(store)
            cancelled = sweep_waiting_device_timeouts(cfg, automation_service=svc)

            self.assertEqual(cancelled, 1)
            self.assertEqual(store.get_run("r_old")["status"], RUN_STATUS_CANCELLED)
            self.assertEqual(store.get_run("r_new")["status"], RUN_STATUS_WAITING_DEVICE)


class ActiveStageTimeoutTests(unittest.TestCase):
    def test_stage_entry_time_survives_poll_updates_and_cancels_safe_stages(self):
        from features.automation.models import (
            RUN_STATUS_CANCELLED,
            RUN_STATUS_REPORT_COLLECTING,
            RUN_STATUS_TEST_RUNNING,
            AutomationRunCreateRequest,
        )
        from features.automation.repository import AutomationStore
        from features.automation.worker import sweep_active_stage_timeouts

        cfg = {
            "executor": "stub",
            "test_timeout_seconds": 3600,
            "report_collection_timeout_seconds": 600,
        }
        long_ago = _iso(datetime.now(timezone.utc) - timedelta(hours=2))

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            request = AutomationRunCreateRequest(
                profile_id="p",
                artifact_path="/tmp/x",
                devices=["S1"],
                test_plan={"test_type": "CTS"},
            )

            for run_id, stage in (
                ("r_test_old", RUN_STATUS_TEST_RUNNING),
                ("r_report_old", RUN_STATUS_REPORT_COLLECTING),
                ("r_test_new", RUN_STATUS_TEST_RUNNING),
            ):
                store.create_run(request.to_run_dict(run_id))
                store.update_run(run_id, status=stage, current_stage=stage)
                event = store.append_event(run_id, stage, "info", "entered stage")
                if run_id.endswith("_old"):
                    with store._connect() as conn:
                        conn.execute(
                            "UPDATE automation_run_events SET created_at = ? WHERE id = ?",
                            (long_ago, event["id"]),
                        )
                    # Polling refreshes updated_at; timeout must still use the
                    # first stage event instead of this recent heartbeat.
                    store.update_run(run_id, current_stage=stage)

            service = _FakeAutomationService(store)
            counts = sweep_active_stage_timeouts(cfg, automation_service=service)

            self.assertEqual(counts, {"test_running": 1, "report_collecting": 1})
            self.assertEqual(store.get_run("r_test_old")["status"], RUN_STATUS_CANCELLED)
            self.assertEqual(store.get_run("r_report_old")["status"], RUN_STATUS_CANCELLED)
            self.assertEqual(store.get_run("r_test_new")["status"], RUN_STATUS_TEST_RUNNING)


class DeviceSelectorTests(unittest.TestCase):
    def _run(self, devices=None, selector=None):
        import json
        plan = {"test_type": "CTS"}
        if selector is not None:
            plan["device_selector"] = selector
        return {
            "id": "r1",
            "devices_json": json.dumps(devices or []),
            "test_plan_json": json.dumps(plan),
        }

    def test_manual_devices_override(self):
        from features.automation.device_selector import DeviceSelector

        selector = DeviceSelector(_FakeDeviceManager([]), _FakeLockManager())
        result = selector.select(self._run(devices=[{"serial": "MANUAL1"}]))
        self.assertTrue(result["success"])
        self.assertEqual([d["serial"] for d in result["devices"]], ["MANUAL1"])

    def test_picks_idle_devices_up_to_min_count(self):
        from features.automation.device_selector import DeviceSelector

        dm = _FakeDeviceManager(["S1", "S2", "S3"])
        lm = _FakeLockManager({"S2"})  # S2 is busy
        selector = DeviceSelector(dm, lm)
        result = selector.select(self._run(selector={"min_count": 2}))
        self.assertTrue(result["success"])
        serials = [d["serial"] for d in result["devices"]]
        self.assertIn("S1", serials)
        self.assertIn("S3", serials)
        self.assertNotIn("S2", serials)
        self.assertEqual(len(serials), 2)

    def test_returns_retry_when_insufficient(self):
        from features.automation.device_selector import DeviceSelector

        dm = _FakeDeviceManager(["S1"])
        selector = DeviceSelector(dm, _FakeLockManager())
        result = selector.select(self._run(selector={"min_count": 2}))
        self.assertFalse(result["success"])
        self.assertTrue(result["retry"])

    def test_serial_prefix_filter(self):
        from features.automation.device_selector import DeviceSelector

        dm = _FakeDeviceManager(["RK001", "QW002", "RK003"])
        selector = DeviceSelector(dm, _FakeLockManager())
        result = selector.select(self._run(selector={"min_count": 1, "serial_prefix": "RK"}))
        self.assertTrue(result["success"])
        self.assertEqual(result["devices"][0]["serial"], "RK001")


class RunTickSyncTests(unittest.TestCase):
    def test_tick_advances_runs_and_polls_builds(self):
        from features.automation.models import RUN_STATUS_COMPLETED, AutomationRunCreateRequest
        from features.automation.repository import AutomationStore
        from features.automation.worker import run_tick_sync
        from features.build import BuildStore

        cfg = {
            "stale_build_seconds": 7200,
            "stale_run_seconds": 86400,
            "waiting_device_timeout_seconds": 1800,
            "executor": "stub",
        }
        with TemporaryDirectory() as tmp:
            build_store = BuildStore(Path(tmp) / "build.sqlite3")
            now = _iso(datetime.now(timezone.utc))
            _make_build_job(build_store, job_id="j1", status="running", started_at=now)
            build_svc = _FakeBuildService(build_store)

            auto_store = AutomationStore(Path(tmp) / "automation.sqlite3")
            auto_store.create_run(AutomationRunCreateRequest(
                profile_id="p", artifact_path="/tmp/x", devices=["S1"],
                test_plan={"test_type": "CTS"},
            ).to_run_dict("r1"))
            auto_svc = _FakeAutomationService(auto_store)

            # First tick: stale sweep runs, build polled, run advances to completed
            # (StubAutomationExecutor completes in one advance_next per tick;
            # worker_tick loops until no run remains, so a single run finishes).
            result = run_tick_sync(cfg, automation_service=auto_svc, build_service=build_svc)

            self.assertEqual(result["polled_builds"], 1)
            self.assertGreaterEqual(result["advanced_runs"], 1)
            self.assertEqual(auto_store.get_run("r1")["status"], RUN_STATUS_COMPLETED)
            self.assertEqual(build_svc.polled, ["j1"])

    def test_tick_honors_build_poll_interval(self):
        from features.automation import worker
        from features.automation.repository import AutomationStore
        from features.build import BuildStore

        cfg = {
            "build_poll_interval_seconds": 60,
            "stale_build_seconds": 7200,
            "stale_run_seconds": 86400,
            "waiting_device_timeout_seconds": 1800,
            "executor": "stub",
        }
        previous_poll = worker._last_build_poll_at
        previous_swept = worker._stale_swept
        try:
            worker._last_build_poll_at = 0.0
            worker._stale_swept = True
            with TemporaryDirectory() as tmp:
                build_store = BuildStore(Path(tmp) / "build.sqlite3")
                now = _iso(datetime.now(timezone.utc))
                _make_build_job(
                    build_store, job_id="j1", status="running", started_at=now
                )
                build_svc = _FakeBuildService(build_store)
                auto_svc = _FakeAutomationService(
                    AutomationStore(Path(tmp) / "automation.sqlite3")
                )

                first = worker.run_tick_sync(
                    cfg, automation_service=auto_svc, build_service=build_svc
                )
                second = worker.run_tick_sync(
                    cfg, automation_service=auto_svc, build_service=build_svc
                )

            self.assertEqual(first["polled_builds"], 1)
            self.assertEqual(second["polled_builds"], 0)
            self.assertEqual(build_svc.polled, ["j1"])
        finally:
            worker._last_build_poll_at = previous_poll
            worker._stale_swept = previous_swept


class WorkerLifecycleTests(unittest.TestCase):
    def test_start_resets_stale_sweep_guard(self):
        from features.automation import worker

        previous_task = worker._task
        previous_swept = worker._stale_swept
        previous_poll = worker._last_build_poll_at
        worker._task = None
        worker._stale_swept = True
        worker._last_build_poll_at = 42.0
        task = MagicMock()
        try:
            with patch.object(
                worker, "_load_worker_config", return_value={"enabled": True}
            ), patch.object(worker.asyncio, "create_task", return_value=task) as create:
                self.assertIs(worker.start_automation_worker(), task)
            create.call_args.args[0].close()
            self.assertFalse(worker._stale_swept)
            self.assertEqual(worker._last_build_poll_at, 0.0)
        finally:
            worker._task = previous_task
            worker._stale_swept = previous_swept
            worker._last_build_poll_at = previous_poll


if __name__ == "__main__":
    unittest.main()
