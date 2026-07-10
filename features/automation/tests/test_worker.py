"""Tests for the background automation worker and device selector.

The worker functions accept injected automation/build services so tests do not
touch the module-level singletons.
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


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

            result = stale_sweep_once(cfg, automation_service=auto_svc, build_service=build_svc)

            self.assertEqual(result["builds_marked"], 1)
            self.assertEqual(build_store.get_job("j_old")["status"], JOB_FAILED)
            self.assertEqual(build_store.get_job("j_new")["status"], "running")
            self.assertEqual(auto_store.get_run("r_old")["status"], RUN_STATUS_CANCELLED)


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


if __name__ == "__main__":
    unittest.main()
