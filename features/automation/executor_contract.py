"""Automation executor interface and deterministic dry-run adapter."""

from __future__ import annotations

import json
from typing import Any, Protocol


class AutomationExecutor(Protocol):
    def trigger_build(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def poll_build(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def select_artifact(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def select_devices(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def flash(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def start_test(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def poll_test(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def cancel(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def collect_report(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def analyze_report(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def report_result(self, run: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class StubAutomationExecutor:
    """Deterministic executor for orchestration tests and dry runs."""

    def __init__(self, fail_stage: str = ""):
        self.fail_stage = fail_stage

    def _result(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_stage == stage:
            return {"success": False, "error": f"{stage} failed by stub"}
        return {"success": True, **payload}

    def select_devices(self, run: dict[str, Any]) -> dict[str, Any]:
        devices = json.loads(run.get("devices_json") or "[]")
        return self._result("select_devices", {"devices": devices})

    def trigger_build(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("trigger_build", {
            "jenkins_queue_url": f"stub://jenkins/queue/{run.get('id', 'run')}",
            "jenkins_build_number": "1",
            "jenkins_build_url": f"stub://jenkins/{run.get('jenkins_job', 'job')}/1/",
        })

    def poll_build(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("poll_build", {
            "building": False,
            "result": "SUCCESS",
            "jenkins_build_number": run.get("jenkins_build_number") or "1",
            "jenkins_build_url": run.get("jenkins_build_url")
            or f"stub://jenkins/{run.get('jenkins_job', 'job')}/1/",
        })

    def select_artifact(self, run: dict[str, Any]) -> dict[str, Any]:
        job = run.get("jenkins_job", "job")
        number = run.get("jenkins_build_number") or "1"
        return self._result("select_artifact", {
            "artifact_url": f"stub://jenkins/{job}/{number}/update.img",
            "artifact_path": run.get("artifact_path", ""),
        })

    def flash(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("flash", {"artifact_path": run.get("artifact_path", "")})

    def start_test(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result(
            "start_test",
            {"test_plan": json.loads(run.get("test_plan_json") or "{}")},
        )

    def poll_test(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("poll_test", {"running": False})

    def cancel(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("cancel", {"cancelled": True})

    def collect_report(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result(
            "collect_report", {"report_timestamp": f"stub_report_{run['id']}"}
        )

    def analyze_report(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("analyze_report", {"summary": {"total": 1, "fail": 0}})

    def report_result(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._result("report_result", {
            "comment": (
                f"Automation run {run['id']} completed. "
                f"Report: {run.get('report_timestamp', '')}"
            ),
        })
