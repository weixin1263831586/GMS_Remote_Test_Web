"""Models for durable GMS ATS automation runs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


RUN_STATUS_QUEUED = "queued"
RUN_STATUS_JENKINS_QUEUED = "jenkins_queued"
RUN_STATUS_JENKINS_BUILDING = "jenkins_building"
RUN_STATUS_ARTIFACT_READY = "artifact_ready"
RUN_STATUS_WAITING_DEVICE = "waiting_device"
RUN_STATUS_DEVICE_LOCKED = "device_locked"
RUN_STATUS_FLASHING = "flashing"
RUN_STATUS_FLASH_VERIFIED = "flash_verified"
RUN_STATUS_TESTING = "testing"
RUN_STATUS_TEST_RUNNING = "test_running"
RUN_STATUS_REPORT_COLLECTING = "report_collecting"
RUN_STATUS_ANALYZING = "analyzing"
RUN_STATUS_REPORTING = "reporting"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_JENKINS_FAILED = "jenkins_failed"
RUN_STATUS_ARTIFACT_MISSING = "artifact_missing"
RUN_STATUS_FLASH_FAILED = "flash_failed"
RUN_STATUS_TEST_FAILED = "test_failed"
RUN_STATUS_ANALYSIS_FAILED = "analysis_failed"
RUN_STATUS_REPORTING_FAILED = "reporting_failed"

TERMINAL_STATUSES = {
    RUN_STATUS_COMPLETED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_JENKINS_FAILED,
    RUN_STATUS_ARTIFACT_MISSING,
    RUN_STATUS_FLASH_FAILED,
    RUN_STATUS_TEST_FAILED,
    RUN_STATUS_ANALYSIS_FAILED,
    RUN_STATUS_REPORTING_FAILED,
}

_RUN_FORWARD_TRANSITIONS = {
    RUN_STATUS_QUEUED: {RUN_STATUS_JENKINS_QUEUED, RUN_STATUS_WAITING_DEVICE},
    RUN_STATUS_JENKINS_QUEUED: {RUN_STATUS_JENKINS_BUILDING, RUN_STATUS_JENKINS_FAILED},
    RUN_STATUS_JENKINS_BUILDING: {RUN_STATUS_ARTIFACT_READY, RUN_STATUS_JENKINS_FAILED},
    RUN_STATUS_ARTIFACT_READY: {RUN_STATUS_WAITING_DEVICE, RUN_STATUS_ARTIFACT_MISSING},
    RUN_STATUS_WAITING_DEVICE: {RUN_STATUS_DEVICE_LOCKED},
    RUN_STATUS_DEVICE_LOCKED: {RUN_STATUS_FLASHING},
    RUN_STATUS_FLASHING: {RUN_STATUS_FLASH_VERIFIED, RUN_STATUS_FLASH_FAILED},
    RUN_STATUS_FLASH_VERIFIED: {RUN_STATUS_TESTING},
    RUN_STATUS_TESTING: {RUN_STATUS_TEST_RUNNING, RUN_STATUS_TEST_FAILED},
    RUN_STATUS_TEST_RUNNING: {RUN_STATUS_REPORT_COLLECTING, RUN_STATUS_TEST_FAILED},
    RUN_STATUS_REPORT_COLLECTING: {RUN_STATUS_ANALYZING, RUN_STATUS_TEST_FAILED},
    RUN_STATUS_ANALYZING: {RUN_STATUS_REPORTING, RUN_STATUS_ANALYSIS_FAILED},
    RUN_STATUS_REPORTING: {RUN_STATUS_COMPLETED, RUN_STATUS_REPORTING_FAILED},
}


def validate_run_transition(from_status: str, to_status: str) -> None:
    if from_status == to_status:
        return
    if from_status in TERMINAL_STATUSES:
        raise ValueError(f"terminal automation run cannot transition: {from_status} -> {to_status}")
    allowed = set(_RUN_FORWARD_TRANSITIONS.get(from_status, set()))
    allowed.update({RUN_STATUS_CANCELLED, RUN_STATUS_FAILED})
    if to_status not in allowed:
        raise ValueError(f"invalid automation run transition: {from_status} -> {to_status}")


def utc_now_iso() -> str:
    # 亚秒精度用于公平排序同一轮次内推进的任务。
    return datetime.utcnow().isoformat(timespec="microseconds") + "Z"


def normalize_devices(devices: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for item in devices or []:
        if isinstance(item, str):
            serial = item.strip()
            if serial:
                normalized.append({"serial": serial})
        elif isinstance(item, dict):
            serial = str(item.get("serial") or item.get("device") or item.get("id") or "").strip()
            if serial:
                normalized.append({**item, "serial": serial})
    return normalized


class AutomationRunCreateRequest(BaseModel):
    profile_id: str = ""
    source_type: str = "manual"
    source_key: str = ""
    project: str = ""
    branch: str = ""
    gerrit_change_id: str = ""
    gerrit_patchset: str = ""
    gerrit_subject: str = ""
    owner: str = ""
    jenkins_job: str = ""
    artifact_url: str = ""
    artifact_path: str = ""
    devices: list[Any] = Field(default_factory=list)
    test_plan: dict[str, Any] = Field(default_factory=dict)

    def to_run_dict(self, run_id: str) -> dict[str, Any]:
        now = utc_now_iso()
        return {
            "id": run_id,
            "trace_id": run_id,
            "state_version": "1",
            "recovery_count": "0",
            "last_recovered_at": "",
            "source_type": self.source_type or "manual",
            "source_key": self.source_key,
            "profile_id": self.profile_id,
            "project": self.project,
            "branch": self.branch,
            "gerrit_change_id": self.gerrit_change_id,
            "gerrit_patchset": self.gerrit_patchset,
            "gerrit_subject": self.gerrit_subject,
            "owner": self.owner,
            "created_by": "",
            "status": RUN_STATUS_QUEUED,
            "current_stage": RUN_STATUS_QUEUED,
            "jenkins_job": self.jenkins_job,
            "jenkins_queue_url": "",
            "jenkins_build_number": "",
            "jenkins_build_url": "",
            "artifact_url": self.artifact_url,
            "artifact_path": self.artifact_path,
            "build_artifact_id": "",
            "worker_id": str((self.test_plan or {}).get("worker_id") or ""),
            "device_reservation_id": "",
            "flash_stage_id": "",
            "flash_command_id": "",
            "cluster_job_id": "",
            "attempt_id": "",
            "devices_json": json.dumps(normalize_devices(self.devices), ensure_ascii=False, separators=(",", ":")),
            "test_plan_json": json.dumps(self.test_plan or {}, ensure_ascii=False, separators=(",", ":")),
            "report_timestamp": "",
            "report_id": "",
            "result_json": "{}",
            "error": "",
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "finished_at": "",
            "lease_owner": "",
            "lease_expires_at": "",
        }


class AutomationEventCreate(BaseModel):
    run_id: str
    stage: str
    level: str = "info"
    message: str
    payload: dict[str, Any] | None = None
