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

            for _ in range(9):
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
