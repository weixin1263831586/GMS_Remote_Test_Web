import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class AutomationExecutorTests(unittest.TestCase):
    def test_stub_executor_returns_deterministic_stage_results(self):
        from features.automation.executors import StubAutomationExecutor

        executor = StubAutomationExecutor()
        run = {
            "id": "run_1",
            "artifact_path": "/tmp/update.img",
            "devices_json": '[{"serial":"ABC123"}]',
            "test_plan_json": '{"test_type":"CTS"}',
        }

        self.assertEqual(executor.select_devices(run)["success"], True)
        self.assertEqual(executor.flash(run)["success"], True)
        self.assertEqual(executor.start_test(run)["success"], True)
        self.assertEqual(executor.collect_report(run)["report_timestamp"], "stub_report_run_1")


class AutomationOrchestratorTests(unittest.TestCase):
    def test_advance_next_schedules_least_recently_advanced_run(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / 'automation.sqlite3')
            older = AutomationRunCreateRequest().to_run_dict('run_older')
            older['created_at'] = older['updated_at'] = '2026-01-01T00:00:00Z'
            newer = AutomationRunCreateRequest().to_run_dict('run_newer')
            newer['created_at'] = newer['updated_at'] = '2026-01-02T00:00:00Z'
            newer['status'] = newer['current_stage'] = 'waiting_device'
            store.create_run(older)
            store.create_run(newer)

            result = AutomationOrchestrator(store, StubAutomationExecutor()).advance_next()

            self.assertEqual(result['id'], 'run_older')
            self.assertEqual(result['status'], 'waiting_device')
            self.assertEqual(store.get_run('run_newer')['status'], 'waiting_device')

    def test_waiting_run_yields_to_other_active_runs_after_retry(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import AutomationRunCreateRequest
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        class WaitingExecutor(StubAutomationExecutor):
            def select_devices(self, run):
                return {"success": False, "retry": True, "error": "busy"}

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            for run_id, timestamp in (
                ("run_first", "2026-01-01T00:00:00Z"),
                ("run_second", "2026-01-02T00:00:00Z"),
            ):
                data = AutomationRunCreateRequest().to_run_dict(run_id)
                data["status"] = data["current_stage"] = "waiting_device"
                data["created_at"] = data["updated_at"] = timestamp
                store.create_run(data)
            orchestrator = AutomationOrchestrator(store, WaitingExecutor())

            first = orchestrator.advance_next()
            second = orchestrator.advance_next()

            self.assertEqual(first["id"], "run_first")
            self.assertEqual(second["id"], "run_second")

    def test_stale_transition_cannot_revive_a_cancelled_run(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import RUN_STATUS_CANCELLED, AutomationRunCreateRequest
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest(devices=["ABC123"]).to_run_dict("run_cancelled"))
            stale = store.get_run("run_cancelled")
            store.update_run("run_cancelled", status=RUN_STATUS_CANCELLED, current_stage=RUN_STATUS_CANCELLED)
            orchestrator = AutomationOrchestrator(store, StubAutomationExecutor())

            result = orchestrator._transition(stale, "testing", "stale worker result")

            self.assertEqual(result["status"], RUN_STATUS_CANCELLED)

    def test_real_style_test_start_waits_for_poll_before_collecting_report(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import (
            RUN_STATUS_REPORT_COLLECTING,
            RUN_STATUS_TEST_RUNNING,
            AutomationRunCreateRequest,
        )
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        class PollingExecutor(StubAutomationExecutor):
            def __init__(self):
                super().__init__()
                self.poll_count = 0

            def poll_test(self, run):
                self.poll_count += 1
                return {"success": True, "running": self.poll_count == 1}

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            data = AutomationRunCreateRequest(
                artifact_path="/tmp/update.img", devices=["ABC123"], test_plan={"test_type": "CTS"}
            ).to_run_dict("run_poll")
            data["status"] = "testing"
            data["current_stage"] = "testing"
            store.create_run(data)
            orchestrator = AutomationOrchestrator(store, PollingExecutor())

            self.assertEqual(orchestrator.advance_run("run_poll")["status"], RUN_STATUS_TEST_RUNNING)
            self.assertEqual(orchestrator.advance_run("run_poll")["status"], RUN_STATUS_TEST_RUNNING)
            self.assertEqual(orchestrator.advance_run("run_poll")["status"], RUN_STATUS_REPORT_COLLECTING)

    def test_orchestrator_advances_manual_run_to_completed(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import RUN_STATUS_COMPLETED, AutomationRunCreateRequest
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest(
                profile_id="manual-smoke",
                artifact_path="/tmp/update.img",
                devices=["ABC123"],
                test_plan={"test_type": "CTS"},
            ).to_run_dict("run_1"))
            orchestrator = AutomationOrchestrator(store, StubAutomationExecutor())

            for _ in range(10):
                orchestrator.advance_run("run_1")

            run = store.get_run("run_1")
            events = store.list_events("run_1")

            self.assertEqual(run["status"], RUN_STATUS_COMPLETED)
            self.assertEqual(run["report_timestamp"], "stub_report_run_1")
            self.assertIn("reporting", [event["stage"] for event in events])
            self.assertTrue(any(event["stage"] == "completed" for event in events))

    def test_orchestrator_records_flash_failure(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import RUN_STATUS_FLASH_FAILED, AutomationRunCreateRequest
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            store.create_run(AutomationRunCreateRequest(
                profile_id="manual-smoke",
                artifact_path="/tmp/update.img",
                devices=["ABC123"],
                test_plan={"test_type": "CTS"},
            ).to_run_dict("run_1"))
            orchestrator = AutomationOrchestrator(store, StubAutomationExecutor(fail_stage="flash"))

            for _ in range(4):
                orchestrator.advance_run("run_1")

            run = store.get_run("run_1")

            self.assertEqual(run["status"], RUN_STATUS_FLASH_FAILED)
            self.assertIn("flash failed by stub", run["error"])

    def test_orchestrator_records_analysis_failure_stage(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import (
            RUN_STATUS_ANALYSIS_FAILED,
            AutomationRunCreateRequest,
        )
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            data = AutomationRunCreateRequest().to_run_dict("run_analysis")
            data["status"] = data["current_stage"] = "analyzing"
            data["report_timestamp"] = "report-1"
            store.create_run(data)

            run = AutomationOrchestrator(
                store, StubAutomationExecutor(fail_stage="analyze_report")
            ).advance_run("run_analysis")

            self.assertEqual(run["status"], RUN_STATUS_ANALYSIS_FAILED)

    def test_reporting_failure_does_not_send_notifications_twice(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import (
            RUN_STATUS_REPORTING_FAILED,
            AutomationRunCreateRequest,
        )
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        class FailingReporter(StubAutomationExecutor):
            calls = 0

            def report_result(self, run):
                self.calls += 1
                return {
                    "success": False,
                    "error": "required notification failed",
                    "notifications": {"ok": False},
                }

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            data = AutomationRunCreateRequest().to_run_dict("run_reporting")
            data["status"] = data["current_stage"] = "reporting"
            store.create_run(data)
            executor = FailingReporter()

            run = AutomationOrchestrator(store, executor).advance_run("run_reporting")

            self.assertEqual(run["status"], RUN_STATUS_REPORTING_FAILED)
            self.assertEqual(executor.calls, 1)

    def test_orchestrator_runs_jenkins_stages_before_device_stages(self):
        from features.automation.executors import StubAutomationExecutor
        from features.automation.models import RUN_STATUS_WAITING_DEVICE, AutomationRunCreateRequest
        from features.automation.orchestrator import AutomationOrchestrator
        from features.automation.repository import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            run_data = AutomationRunCreateRequest(
                profile_id="gerrit-smoke",
                source_type="gerrit_webhook",
                source_key="gerrit:project:123:7:gerrit-smoke",
                devices=["ABC123"],
                test_plan={
                    "test_type": "CTS",
                    "jenkins": {"parameters": {"GERRIT_CHANGE": "123"}, "artifact_pattern": ".*update.*\\.img$"},
                },
            ).to_run_dict("run_jenkins")
            run_data["jenkins_job"] = "GMS_BUILD"
            store.create_run(run_data)
            orchestrator = AutomationOrchestrator(store, StubAutomationExecutor())

            statuses = [orchestrator.advance_run("run_jenkins")["status"] for _ in range(4)]
            run = store.get_run("run_jenkins")

            self.assertEqual(statuses[:4], ["jenkins_queued", "jenkins_building", "artifact_ready", "waiting_device"])
            self.assertEqual(run["status"], RUN_STATUS_WAITING_DEVICE)
            self.assertEqual(run["jenkins_build_number"], "1")
            self.assertEqual(run["artifact_url"], "stub://jenkins/GMS_BUILD/1/update.img")
