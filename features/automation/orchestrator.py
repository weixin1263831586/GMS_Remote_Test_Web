"""State machine for automation runs."""

from __future__ import annotations

import json
from typing import Any

from features.automation.executors import AutomationExecutor
from features.automation.models import (
    RUN_STATUS_ANALYZING,
    RUN_STATUS_ARTIFACT_MISSING,
    RUN_STATUS_ARTIFACT_READY,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_DEVICE_LOCKED,
    RUN_STATUS_FAILED,
    RUN_STATUS_FLASH_FAILED,
    RUN_STATUS_FLASH_VERIFIED,
    RUN_STATUS_FLASHING,
    RUN_STATUS_JENKINS_BUILDING,
    RUN_STATUS_JENKINS_FAILED,
    RUN_STATUS_JENKINS_QUEUED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_REPORT_COLLECTING,
    RUN_STATUS_REPORTING,
    RUN_STATUS_REPORTING_FAILED,
    RUN_STATUS_TEST_FAILED,
    RUN_STATUS_TESTING,
    RUN_STATUS_WAITING_DEVICE,
    TERMINAL_STATUSES,
    utc_now_iso,
)
from features.automation.repository import AutomationStore


def _run_has_build(run: dict[str, Any]) -> bool:
    try:
        plan = json.loads(run.get("test_plan_json") or "{}")
    except json.JSONDecodeError:
        return False
    build = plan.get("build") if isinstance(plan.get("build"), dict) else {}
    return bool(build.get("provider") or build.get("server_id") or build.get("template_id"))


class AutomationOrchestrator:
    def __init__(self, store: AutomationStore, executor: AutomationExecutor):
        self.store = store
        self.executor = executor

    def advance_next(self) -> dict[str, Any] | None:
        for run in self.store.list_runs(limit=100):
            if run["status"] not in TERMINAL_STATUSES:
                return self.advance_run(run["id"])
        return None

    def advance_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError(f"automation run not found: {run_id}")
        if run["status"] in TERMINAL_STATUSES:
            return run

        status = run["status"]
        if status == RUN_STATUS_QUEUED:
            if run.get("jenkins_job") or _run_has_build(run):
                return self._transition(run, RUN_STATUS_JENKINS_QUEUED, "Run is queued for firmware build")
            return self._transition(run, RUN_STATUS_WAITING_DEVICE, "Run is waiting for device selection")
        if status == RUN_STATUS_JENKINS_QUEUED:
            result = self.executor.trigger_build(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_JENKINS_FAILED, result.get("error", "Jenkins trigger failed"), result)
            return self._transition(
                run,
                RUN_STATUS_JENKINS_BUILDING,
                "Firmware build triggered",
                result,
                jenkins_queue_url=result.get("jenkins_queue_url", ""),
                jenkins_build_number=result.get("jenkins_build_number", ""),
                jenkins_build_url=result.get("jenkins_build_url", ""),
            )
        if status == RUN_STATUS_JENKINS_BUILDING:
            result = self.executor.poll_build(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_JENKINS_FAILED, result.get("error", "Firmware build failed"), result)
            if result.get("building"):
                self.store.append_event(run["id"], RUN_STATUS_JENKINS_BUILDING, "info", "Firmware build is still running", result)
                return run
            if result.get("result") and result.get("result") != "SUCCESS":
                return self._fail(run, RUN_STATUS_JENKINS_FAILED, f"Jenkins build result: {result.get('result')}", result)
            return self._transition(
                run,
                RUN_STATUS_ARTIFACT_READY,
                "Firmware build completed",
                result,
                jenkins_queue_url=result.get("jenkins_queue_url", run.get("jenkins_queue_url", "")),
                jenkins_build_number=result.get("jenkins_build_number", run.get("jenkins_build_number", "")),
                jenkins_build_url=result.get("jenkins_build_url", run.get("jenkins_build_url", "")),
                artifact_url=result.get("artifact_url", run.get("artifact_url", "")),
                artifact_path=result.get("artifact_path", run.get("artifact_path", "")),
            )
        if status == RUN_STATUS_ARTIFACT_READY:
            result = self.executor.select_artifact(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_ARTIFACT_MISSING, result.get("error", "artifact missing"), result)
            return self._transition(
                run,
                RUN_STATUS_WAITING_DEVICE,
                "Build artifact selected",
                result,
                artifact_url=result.get("artifact_url", ""),
                artifact_path=result.get("artifact_path", run.get("artifact_path", "")),
            )
        if status == RUN_STATUS_WAITING_DEVICE:
            result = self.executor.select_devices(run)
            if not result.get("success"):
                if result.get("retry"):
                    # No idle devices yet — stay in waiting_device and let the
                    # worker retry next tick. Worker enforces a timeout.
                    self.store.append_event(
                        run["id"],
                        RUN_STATUS_WAITING_DEVICE,
                        "info",
                        f"Waiting for devices: {result.get('error', '')}",
                        result,
                    )
                    return run
                return self._fail(run, RUN_STATUS_FAILED, result.get("error", "device selection failed"), result)
            devices = [{"serial": item.get("serial", "")} for item in result.get("devices") or []]
            return self._transition(
                run,
                RUN_STATUS_DEVICE_LOCKED,
                "Devices selected and locked",
                result,
                devices_json=json.dumps(devices, ensure_ascii=False, separators=(",", ":")),
            )
        if status == RUN_STATUS_DEVICE_LOCKED:
            return self._transition(run, RUN_STATUS_FLASHING, "Starting firmware flash")
        if status == RUN_STATUS_FLASHING:
            result = self.executor.flash(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_FLASH_FAILED, result.get("error", "flash failed"), result)
            return self._transition(run, RUN_STATUS_FLASH_VERIFIED, "Firmware flash completed", result)
        if status == RUN_STATUS_FLASH_VERIFIED:
            return self._transition(run, RUN_STATUS_TESTING, "Starting GMS test")
        if status == RUN_STATUS_TESTING:
            result = self.executor.start_test(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_TEST_FAILED, result.get("error", "test failed"), result)
            return self._transition(run, RUN_STATUS_REPORT_COLLECTING, "Test completed, collecting report", result)
        if status == RUN_STATUS_REPORT_COLLECTING:
            result = self.executor.collect_report(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_TEST_FAILED, result.get("error", "report collection failed"), result)
            return self._transition(
                run,
                RUN_STATUS_ANALYZING,
                "Report collected",
                result,
                report_timestamp=result.get("report_timestamp", ""),
            )
        if status == RUN_STATUS_ANALYZING:
            result = self.executor.analyze_report(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_FAILED, result.get("error", "analysis failed"), result)
            return self._transition(
                run,
                RUN_STATUS_REPORTING,
                "Report analysis completed",
                result,
                result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            )
        if status == RUN_STATUS_REPORTING:
            result = self.executor.report_result(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_REPORTING_FAILED, result.get("error", "result reporting failed"), result)
            return self._transition(
                run,
                RUN_STATUS_COMPLETED,
                "Automation run completed",
                result,
                finished_at=utc_now_iso(),
            )

        return self._fail(run, RUN_STATUS_FAILED, f"unsupported status: {status}", {})

    def cancel_run(self, run_id: str, reason: str = "cancelled by user") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError(f"automation run not found: {run_id}")
        if run["status"] in TERMINAL_STATUSES:
            return run
        return self._transition(run, RUN_STATUS_CANCELLED, reason, {}, finished_at=utc_now_iso())

    def _transition(self, run: dict[str, Any], status: str, message: str, payload: dict[str, Any] | None = None, **updates: Any) -> dict[str, Any]:
        update_payload = {"status": status, "current_stage": status, **updates}
        if run["started_at"] == "" and status != RUN_STATUS_QUEUED:
            update_payload["started_at"] = utc_now_iso()
        updated = self.store.update_run(run["id"], **update_payload)
        self.store.append_event(run["id"], status, "success" if status == RUN_STATUS_COMPLETED else "info", message, payload or {})
        self._audit(run, run.get("status", ""), status, message)
        return updated

    def _fail(self, run: dict[str, Any], status: str, error: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        updated = self.store.update_run(
            run["id"],
            status=status,
            current_stage=status,
            error=error,
            finished_at=utc_now_iso(),
        )
        self.store.append_event(run["id"], status, "error", error, payload or {})
        self._audit(run, run.get("status", ""), status, error)
        return updated

    @staticmethod
    def _audit(run: dict[str, Any], from_status: str, to_status: str, detail: str) -> None:
        """Best-effort security audit of a state transition. Never raises."""
        try:
            from features.system import security_audit_logger

            security_audit_logger.log_event({
                "action_type": "automation_transition",
                "source": "automation",
                "operation": f"run {run.get('id', '')} {from_status} -> {to_status}",
                "run_id": run.get("id", ""),
                "profile_id": run.get("profile_id", ""),
                "from_status": from_status,
                "to_status": to_status,
                "detail": detail,
                "status_code": 200,
            })
        except Exception:
            pass
