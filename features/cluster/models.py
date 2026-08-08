from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import ClusterConfig


class WorkerRegistration(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    name: str = ""
    hostname: str = ""
    address: str = ""
    agent_version: str = ""
    max_jobs: int = Field(default_factory=lambda: ClusterConfig.load().default_max_jobs, ge=1, le=32)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    session_id: str = Field(default="", max_length=128)


class WorkerDevice(BaseModel):
    serial: str = Field(min_length=1, max_length=256)
    transport: str = "local_usb"
    state: str = "available"
    properties: dict[str, Any] = Field(default_factory=dict)


class WorkerSuite(BaseModel):
    suite_type: str
    suite_version: str = ""
    suite_key: str = ""
    tools_path: str
    checksum: str = ""
    size_bytes: int = 0
    available: bool = True


class RunningWorkerJob(BaseModel):
    worker_job_id: str
    job_id: str
    attempt_id: str
    status: str
    pid: int | None = None
    devices: list[str] = Field(default_factory=list)
    source: Literal["managed", "external"] = "managed"
    suite_type: str = ""
    command: str = ""
    started_at: str = ""
    elapsed_seconds: int = Field(default=0, ge=0)
    cpu_percent: float = Field(default=0, ge=0)
    rss_mb: float = Field(default=0, ge=0)
    process_count: int = Field(default=1, ge=1)
    log_path: str = ""
    last_output_age_seconds: int | None = Field(default=None, ge=0)
    warning: str = ""
    trace_id: str = ""
    operation_id: str = ""


class WorkerCommandState(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    status: Literal["accepted", "running", "completed", "failed", "cancelled"]
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class WorkerHeartbeat(BaseModel):
    agent_version: str = ""
    cpu_percent: float = 0
    memory_percent: float = 0
    memory_total_gb: float = 0
    memory_available_gb: float = 0
    load_1m: float = 0
    disk_free_gb: float = 0
    disk_total_gb: float = 0
    running_jobs: list[RunningWorkerJob] = Field(default_factory=list)
    devices: list[WorkerDevice] = Field(default_factory=list)
    suites: list[WorkerSuite] | None = None
    session_id: str = Field(default="", max_length=128)
    connection_generation: int = Field(default=0, ge=0)
    command_states: list[WorkerCommandState] = Field(default_factory=list, max_length=200)
    adb_proxy: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = ""


class CommandAck(BaseModel):
    status: Literal["accepted", "running", "completed", "failed", "cancelled"]
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class CommandCreate(BaseModel):
    worker_id: str
    command_type: str
    job_id: str = ""
    attempt_id: str = ""
    operation_id: str = ""
    trace_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ClusterJobCreate(BaseModel):
    worker_id: str
    suite_key: str = ""
    suite_path: str = ""
    devices: list[str] = Field(default_factory=list)
    argv: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    owner_id: str = ""
    source_type: str = "manual"
    priority: int = 100
    device_count: int = Field(default=1, ge=1, le=32)


class ClusterDeviceAction(BaseModel):
    worker_id: str
    devices: list[str] = Field(min_length=1, max_length=32)
    action: Literal["reboot", "reboot_bootloader", "remount", "get_properties",
                    "bootloader_status", "bootloader_lock", "bootloader_unlock",
                    "wifi", "screenshot", "layout", "tap", "scrcpy_start",
                    "packages_with_path", "packages_all", "features", "props",
                    "config_explore", "override_status", "override_apply",
                    "override_revert", "override_disable_verity",
                    "override_enable_verity", "override_reboot"]
    x: int | None = Field(default=None, ge=0, le=10000)
    y: int | None = Field(default=None, ge=0, le=10000)
    ssid: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=256)
    package: str = Field(default="android", max_length=255)
    name_filter: str = Field(default="", max_length=255)
    type_filter: str = Field(default="", max_length=64)
    with_effective: bool = False
    effective_limit: int = Field(default=0, ge=0, le=5000)
    target_package: str = Field(default="android", max_length=255)
    entries: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class ClusterSuiteDownload(BaseModel):
    worker_id: str
    url: str = Field(min_length=8, max_length=4096)
    filename: str = Field(default="", max_length=255)
    size_bytes: int = Field(default=0, ge=0)


class ClusterSuiteExtract(BaseModel):
    worker_id: str
    archive_path: str
    target_dir_name: str = Field(pattern=r"^[A-Za-z0-9._+-]+$", min_length=1, max_length=200)


class TransferComplete(BaseModel):
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunk_count: int = Field(ge=1, le=100000)


class ArtifactUploadInit(BaseModel):
    attempt_id: str
    filename: str
    artifact_type: str = "file"
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunk_size: int = Field(ge=64 * 1024, le=64 * 1024 * 1024)
    chunk_count: int = Field(ge=1, le=100000)


class ArtifactUploadComplete(BaseModel):
    chunk_count: int = Field(ge=1, le=100000)


class JobEvent(BaseModel):
    sequence: int = Field(ge=0)
    event_type: str = "log"
    source: str = "worker"
    level: str = "info"
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class JobEventBatch(BaseModel):
    attempt_id: str
    events: list[JobEvent]
