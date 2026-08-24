"""State machine for automation runs."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from typing import Any

from features.automation.executors import AutomationExecutor
from features.automation.models import (
    RUN_STATUS_ANALYSIS_FAILED,
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
    RUN_STATUS_TEST_RUNNING,
    RUN_STATUS_TESTING,
    RUN_STATUS_WAITING_DEVICE,
    TERMINAL_STATUSES,
    utc_now_iso,
)
from features.automation.repository import AutomationStore


logger = logging.getLogger(__name__)


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
        self.claim_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def advance_next(self) -> dict[str, Any] | None:
        run = self.store.claim_next_active(self.claim_owner)
        if not run:
            return None
        try:
            return self._advance_with_heartbeat(run)
        finally:
            self.store.release_claim(run["id"], self.claim_owner)

    def advance_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError(f"automation run not found: {run_id}")
        if run["status"] in TERMINAL_STATUSES:
            return run

        if not self.store.claim_run(run_id, self.claim_owner):
            return self.store.get_run(run_id) or run
        try:
            return self._advance_with_heartbeat(self.store.get_run(run_id) or run)
        finally:
            self.store.release_claim(run_id, self.claim_owner)

    def _advance_with_heartbeat(self, run: dict[str, Any]) -> dict[str, Any]:
        """Keep the run claim alive during long build, upload and analysis calls."""
        stopped = threading.Event()

        def renew() -> None:
            while not stopped.wait(30):
                try:
                    if not self.store.renew_claim(run["id"], self.claim_owner):
                        return
                except Exception:
                    logger.warning(
                        "Failed to renew automation claim for %s", run["id"],
                        exc_info=True,
                    )

        heartbeat = threading.Thread(
            target=renew,
            name=f"AutomationClaim-{run['id']}",
            daemon=True,
        )
        heartbeat.start()
        try:
            return self._advance_run(run)
        finally:
            stopped.set()
            heartbeat.join(timeout=1)

    def _advance_run(self, run: dict[str, Any]) -> dict[str, Any]:
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
                return self._stay(run, "Firmware build is still running", result)
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
                build_artifact_id=(
                    (result.get("artifact") or {}).get("id", "")
                    or (
                        f"{result.get('jenkins_build_number', '')}:"
                        f"{result.get('artifact_path') or result.get('artifact_url') or ''}"
                    ).strip(":")
                ),
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
                    return self._stay(
                        run, f"Waiting for devices: {result.get('error', '')}", result
                    )
                return self._fail(run, RUN_STATUS_FAILED, result.get("error", "device selection failed"), result)
            devices = [{"serial": item.get("serial", "")} for item in result.get("devices") or []]
            return self._transition(
                run,
                RUN_STATUS_DEVICE_LOCKED,
                "Devices selected and locked",
                result,
                devices_json=json.dumps(devices, ensure_ascii=False, separators=(",", ":")),
                worker_id=result.get("worker_id", run.get("worker_id", "")),
                device_reservation_id=result.get("reservation_id", ""),
            )
        if status == RUN_STATUS_DEVICE_LOCKED:
            return self._transition(run, RUN_STATUS_FLASHING, "Starting firmware flash")
        if status == RUN_STATUS_FLASHING:
            result = self.executor.flash(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_FLASH_FAILED, result.get("error", "flash failed"), result)
            flash_updates = {
                "flash_stage_id": result.get("stage_id", run.get("flash_stage_id", "")),
                "flash_command_id": result.get("command_id", run.get("flash_command_id", "")),
            }
            if result.get("running"):
                return self._stay(
                    run, "Firmware flash is still running", result, **flash_updates
                )
            return self._transition(
                run, RUN_STATUS_FLASH_VERIFIED, "Firmware flash completed", result,
                **flash_updates,
            )
        if status == RUN_STATUS_FLASH_VERIFIED:
            return self._transition(run, RUN_STATUS_TESTING, "Starting GMS test")
        if status == RUN_STATUS_TESTING:
            result = self.executor.start_test(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_TEST_FAILED, result.get("error", "test failed"), result)
            return self._transition(
                run, RUN_STATUS_TEST_RUNNING, "GMS test accepted and running", result,
                result_json=self._merged_result_json(run, {"test_start": result}),
                cluster_job_id=result.get("cluster_job_id", ""),
                attempt_id=result.get("attempt_id", ""),
            )
        if status == RUN_STATUS_TEST_RUNNING:
            result = self.executor.poll_test(run)
            if not result.get("success"):
                return self._fail(run, RUN_STATUS_TEST_FAILED, result.get("error", "test failed"), result)
            if result.get("running"):
                return self._stay(run, "GMS test is still running", result)
            return self._transition(
                run,
                RUN_STATUS_REPORT_COLLECTING,
                "Test completed, collecting report",
                result,
                report_timestamp=result.get("report_timestamp", ""),
                report_id=result.get("report_id", ""),
            )
        if status == RUN_STATUS_REPORT_COLLECTING:
            result = self.executor.collect_report(run)
            if not result.get("success"):
                if result.get("retry"):
                    return self._stay(run, result.get("error", "Waiting for report indexing"), result)
                return self._fail(run, RUN_STATUS_TEST_FAILED, result.get("error", "report collection failed"), result)
            return self._transition(
                run,
                RUN_STATUS_ANALYZING,
                "Report collected",
                result,
                report_timestamp=result.get("report_timestamp", ""),
                report_id=result.get("report_id", run.get("report_id", "")),
            )
        if status == RUN_STATUS_ANALYZING:
            result = self.executor.analyze_report(run)
            if not result.get("success"):
                return self._fail(
                    run,
                    RUN_STATUS_ANALYSIS_FAILED,
                    result.get("error", "analysis failed"),
                    result,
                )
            return self._transition(
                run,
                RUN_STATUS_REPORTING,
                "Report analysis completed",
                result,
                result_json=self._merged_result_json(run, {"analysis": result}),
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

    def cancel_run(
        self,
        run_id: str,
        reason: str = "cancelled by user",
        *,
        cleanup: bool = True,
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError(f"automation run not found: {run_id}")
        if run["status"] in TERMINAL_STATUSES:
            return run
        if not self.store.claim_run(run_id, self.claim_owner):
            raise RuntimeError("automation run is being advanced; retry cancellation")
        try:
            run = self.store.get_run(run_id) or run
            if run["status"] in TERMINAL_STATUSES:
                return run
            if cleanup:
                result = self.executor.cancel(run)
                if not result.get("success"):
                    self.store.append_event(
                        run["id"], run["status"], "error",
                        result.get("error", "cancel failed"), result,
                    )
                    raise RuntimeError(result.get("error", "cancel failed"))
            else:
                release = getattr(self.executor, "release_resources", None)
                if callable(release):
                    try:
                        release(run)
                    except Exception:
                        logger.warning(
                            "Failed to release resources while cancelling %s",
                            run["id"], exc_info=True,
                        )
            return self._transition(
                run, RUN_STATUS_CANCELLED, reason, {}, finished_at=utc_now_iso()
            )
        finally:
            self.store.release_claim(run_id, self.claim_owner)

    @staticmethod
    def _merged_result_json(run: dict[str, Any], addition: dict[str, Any]) -> str:
        try:
            current = json.loads(run.get("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(addition)
        return json.dumps(current, ensure_ascii=False, separators=(",", ":"))

    def _transition(self, run: dict[str, Any], status: str, message: str, payload: dict[str, Any] | None = None, **updates: Any) -> dict[str, Any]:
        current = self.store.get_run(run["id"])
        if current and current["status"] in TERMINAL_STATUSES:
            return current
        update_payload = {"status": status, "current_stage": status, **updates}
        if run["started_at"] == "" and status != RUN_STATUS_QUEUED:
            update_payload["started_at"] = utc_now_iso()
        updated, applied = self.store.update_run_if_status(
            run["id"], run["status"], **update_payload
        )
        if not applied:
            return updated
        operation_id = f"{run['id']}:transition:{updated.get('state_version', '')}"
        self.store.append_event(
            run["id"],
            status,
            "success" if status == RUN_STATUS_COMPLETED else "info",
            message,
            payload or {},
            event_type="run.transition",
            operation_id=operation_id,
            from_status=run["status"],
            to_status=status,
        )
        self._audit(run, run.get("status", ""), status, message)
        return updated

    def _stay(
        self, run: dict[str, Any], message: str,
        payload: dict[str, Any] | None = None, **updates: Any,
    ) -> dict[str, Any]:
        updated, applied = self.store.update_run_if_status(
            run["id"], run["status"], **updates
        )
        if applied:
            self.store.append_event(
                run["id"], run["status"], "info", message, payload or {},
                event_type="stage.poll",
                operation_id=f"{run['id']}:{run['status']}:poll",
                from_status=run["status"],
                to_status=run["status"],
            )
        return updated

    def _fail(self, run: dict[str, Any], status: str, error: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.store.get_run(run["id"])
        if current and current["status"] in TERMINAL_STATUSES:
            return current
        release = getattr(self.executor, "release_resources", None)
        if callable(release):
            try:
                release(run)
            except Exception:
                logger.warning("Failed to release resources for %s", run["id"], exc_info=True)
        failure_payload = dict(payload or {})
        reporter = getattr(self.executor, "report_result", None)
        if status != RUN_STATUS_REPORTING_FAILED and callable(reporter):
            try:
                failure_payload["notifications"] = reporter(
                    {**run, "status": status, "error": error}
                ).get("notifications", {})
            except Exception:
                logger.warning("Failed to report terminal ATS failure %s", run["id"], exc_info=True)
        updated, applied = self.store.update_run_if_status(
            run["id"], run["status"],
            status=status,
            current_stage=status,
            error=error,
            finished_at=utc_now_iso(),
        )
        if not applied:
            return updated
        self.store.append_event(
            run["id"], status, "error", error, failure_payload,
            event_type="run.transition",
            operation_id=f"{run['id']}:transition:{updated.get('state_version', '')}",
            from_status=run["status"],
            to_status=status,
        )
        self._audit(run, run.get("status", ""), status, error)
        return updated

    @staticmethod
    def _audit(run: dict[str, Any], from_status: str, to_status: str, detail: str) -> None:
        """Best-effort security audit of a state transition. Never raises."""
        try:
            from foundation.security_audit import security_audit_logger

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
            logger.debug("Failed to record automation transition audit", exc_info=True)
