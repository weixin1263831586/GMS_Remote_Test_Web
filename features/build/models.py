from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

TERMINAL_JOB_STATUSES = {JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED}


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class BuildJobCreateRequest(BaseModel):
    server_id: str
    template_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    server_password: str = ""
    source_type: str = "manual"
    source_key: str = ""
    owner: str = ""
    automation_run_id: str = ""


class BuildServerConfig(BaseModel):
    id: str
    name: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    auth: dict[str, Any] = Field(default_factory=dict)
    workspace_root: str = ""
    max_concurrent_jobs: int = 1
    backend: str = "ssh"
    artifact_patterns: list[str] = Field(default_factory=list)


class BuildTemplateConfig(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    server_id: str = ""
    workspace: str = ""
    init_commands: list[str] = Field(default_factory=list)
    command: str
    parameters_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)
    timeout_sec: int = 21600
    artifact_patterns: list[str] = Field(default_factory=list)
    enabled: bool = True
