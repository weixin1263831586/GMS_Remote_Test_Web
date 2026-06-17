"""Models for durable GMS ATS automation runs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

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


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def normalize_devices(devices: List[Any]) -> List[Dict[str, Any]]:
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
    artifact_url: str = ""
    artifact_path: str = ""
    devices: List[Any] = Field(default_factory=list)
    test_plan: Dict[str, Any] = Field(default_factory=dict)

    def to_run_dict(self, run_id: str) -> Dict[str, Any]:
        now = utc_now_iso()
        return {
            "id": run_id,
            "source_type": self.source_type or "manual",
            "source_key": self.source_key,
            "profile_id": self.profile_id,
            "project": self.project,
            "branch": self.branch,
            "gerrit_change_id": self.gerrit_change_id,
            "gerrit_patchset": self.gerrit_patchset,
            "gerrit_subject": self.gerrit_subject,
            "owner": self.owner,
            "status": RUN_STATUS_QUEUED,
            "current_stage": RUN_STATUS_QUEUED,
            "jenkins_job": "",
            "jenkins_queue_url": "",
            "jenkins_build_number": "",
            "jenkins_build_url": "",
            "artifact_url": self.artifact_url,
            "artifact_path": self.artifact_path,
            "devices_json": json.dumps(normalize_devices(self.devices), ensure_ascii=False, separators=(",", ":")),
            "test_plan_json": json.dumps(self.test_plan or {}, ensure_ascii=False, separators=(",", ":")),
            "report_timestamp": "",
            "result_json": "{}",
            "error": "",
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "finished_at": "",
        }


class AutomationEventCreate(BaseModel):
    run_id: str
    stage: str
    level: str = "info"
    message: str
    payload: Optional[Dict[str, Any]] = None
