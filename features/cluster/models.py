from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from foundation.aoa_profile import MAX_PROFILE_COMMANDS
from foundation.device_action_spec import DeviceAction

from .config import ClusterConfig


# Worker 上报的事件 payload 上限（字节，序列化后）。正常事件 payload 为空或
# 少量键值；screenshot 等大负载走独立传输通道，不应经事件批灌入。
_EVENT_PAYLOAD_MAX_BYTES = 8 * 1024


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
    devices: list[str] = Field(default_factory=list, max_length=32)
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


class ExecutionSpec(BaseModel):
    """Structured test execution parameters that replace free-form argv.

    When provided on ClusterJobCreate, the Controller validates the fields and
    builds the Worker argv internally — the browser never supplies raw command
    strings.  The spec is persisted on the job for audit and structured retry.
    """

    test_type: str = Field(min_length=1, max_length=32)
    suite_path: str = Field(min_length=1, max_length=4096)
    module: str = Field(default="", max_length=512)
    test_case: str = Field(default="", max_length=512)
    retry_dir: str = Field(default="", max_length=512)
    devices: list[str] = Field(default_factory=list)
    local_server: str = Field(default="", max_length=256)
    no_retry: bool = False
    copy_remote: bool = False
    shard_count: int = Field(default=0, ge=0, le=32, strict=True)
    preparations: list[str] = Field(default_factory=list, max_length=8)
    queue_timeout_seconds: int = Field(default=0, ge=0, le=604800, strict=True)
    execution_timeout_seconds: int = Field(default=0, ge=0, le=604800, strict=True)
    output_idle_timeout_seconds: int = Field(default=0, ge=0, le=86400, strict=True)


class ClusterJobCreate(BaseModel):
    worker_id: str
    suite_key: str = ""
    suite_path: str = ""
    # Every Job leases devices. An empty request selects device_count devices
    # on either the explicit Worker or the automatically selected Worker.
    devices: list[str] = Field(default_factory=list)
    # argv is derived server-side from execution_spec (or the default listing
    # command); the browser-facing POST /api/cluster/jobs rejects non-empty
    # argv so structured validation cannot be bypassed with raw commands.
    argv: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    owner_id: str = ""
    source_type: str = "manual"
    priority: int = 100
    device_count: int = Field(default=1, ge=1, le=32)
    execution_spec: ExecutionSpec | None = None


class ClusterDeviceAction(BaseModel):
    worker_id: str
    devices: list[str] = Field(min_length=1, max_length=32)
    # DeviceAction 枚举由 DeviceActionSpec 派生——新增 action 只需在
    # device_action_spec._DEVICE_ACTION_SPECS 登记一行。
    action: DeviceAction
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
    # aoa_sequence：内联 AOA 命令序列（官方 AoaTargetPreparer action 语法）
    # 与执行后等待 ADB 恢复的秒数；aoa_probe 忽略两者。上限与 profile
    # 存储层（foundation.aoa_profile.MAX_PROFILE_COMMANDS）保持一致，
    # 避免 recover 在 Controller 侧被 pydantic 拦成 500。
    aoa_commands: list[str] = Field(
        default_factory=list, max_length=MAX_PROFILE_COMMANDS)
    wait_adb_seconds: int = Field(default=0, ge=0, le=600)
    aoa_hid_touch_mode: Literal["pointer", "direct"] = "pointer"
    aoa_hid_settle_seconds: float = Field(default=2, ge=0, le=10)
    aoa_hid_session_mode: Literal["continuous", "per_command"] = "continuous"
    aoa_touch_mapping: dict[str, Any] | None = None
    aoa_source_touch_mapping: dict[str, Any] | None = None
    aoa_mapping_policy: str = Field(default="", max_length=32)
    aoa_semantic_plan: dict[str, Any] | None = None
    aoa_direct_certificate: dict[str, Any] | None = None
    aoa_firmware_sha256: str = Field(default="", max_length=64, pattern=r"^(?:[a-f0-9]{64})?$")
    aoa_debug_session_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    aoa_debug_request_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    aoa_debug_command: str = Field(default="", max_length=1024)
    # aoa_record：userdebug 设备上的录制参数（steps/setup_wizard/tv）。
    aoa_record: dict[str, Any] = Field(default_factory=dict)
    # 自动化重放幂等键（ADR 0022）：非空时远程 Worker 命令按
    # ``automation:{run_id}:{action}`` 派生 operation_id 在数据库层原子
    # 去重，“派发成功但响应丢失”后的重放收敛到同一条命令而不重放
    # USB 调试开关点击。空值保持每请求一条新命令的既有语义；该字段
    # 只参与 Controller 侧去重，不进入 Worker 执行 payload。
    automation_run_id: str = Field(default="", max_length=128)
    usb_port_path: str = Field(default="", max_length=128, pattern=r"^(?:[0-9]+-[0-9]+(?:\.[0-9]+)*)?$")
    # aoa_sequence 回放过程控制句柄（前端生成的一次性 id）：随命令下发，
    # 运行中经 /devices/aoa/playback/{id}/control 暂停/继续/终止。
    playback_control_id: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_aoa_mapping(self):
        from foundation.aoa_calibration import TouchMapping, convert_commands, mapping_options
        from foundation.aoa_profile import parse_record_input_action

        if self.action == "aoa_record_input" and self.aoa_record.get("action") in {"verify_direct", "observe_frame"}:
            self.aoa_record = parse_record_input_action(self.aoa_record)

        normalized = mapping_options(self.model_dump(include={
            "aoa_touch_mapping", "aoa_source_touch_mapping", "aoa_mapping_policy", "aoa_hid_touch_mode",
            "aoa_semantic_plan", "aoa_direct_certificate", "aoa_firmware_sha256"}))
        for key, value in normalized.items():
            setattr(self, key, value)
        if self.aoa_semantic_plan is not None and self.aoa_commands != self.aoa_semantic_plan["source_commands"]:
            raise ValueError("semantic source commands differ from the request command list")
        if self.action == "aoa_sequence" and self.aoa_commands and self.aoa_touch_mapping:
            target = TouchMapping.from_dict(self.aoa_touch_mapping)
            source = TouchMapping.from_dict(self.aoa_source_touch_mapping) if self.aoa_source_touch_mapping else target
            convert_commands(self.aoa_commands, source, target)
        return self


class FirmwareRecoveryPreflight(BaseModel):
    worker_id: str = Field(default="", max_length=128)
    devices: list[str] = Field(min_length=1, max_length=1)
    aoa_profile_id: str = Field(min_length=1, max_length=128)
    aoa_boot_wait_seconds: int = Field(default=60, strict=True, ge=0, le=600)


class ClusterSuiteDownload(BaseModel):
    worker_id: str
    sha256: str = Field(default='', pattern=r'^(?:[a-f0-9]{64})?$')
    url: str = Field(min_length=8, max_length=4096)
    filename: str = Field(default="", max_length=255)
    size_bytes: int = Field(default=0, ge=0)


class ClusterSuiteExtract(BaseModel):
    worker_id: str
    archive_path: str
    target_dir_name: str = Field(pattern=r"^[A-Za-z0-9._+-]+$", min_length=1, max_length=200)


class TransferComplete(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunk_count: int = Field(ge=1, le=100000)


class ReportCopyCreate(BaseModel):
    source_worker_id: str = Field(min_length=1, max_length=128)
    source_suite_path: str = Field(min_length=1, max_length=4096)
    report_name: str = Field(
        pattern=r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+)?(?:_\d+)?$",
        min_length=19,
        max_length=128,
    )
    target_worker_id: str = Field(min_length=1, max_length=128)
    target_suite_path: str = Field(min_length=1, max_length=4096)


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
    event_type: str = Field(default="log", max_length=32)
    source: str = Field(default="worker", max_length=64)
    level: str = Field(default="info", max_length=16)
    # Controller 是分布式服务边界：不依赖 Worker 自觉截断，
    # 单条/批次/负载超限直接 422，防止 token 泄漏或 Agent bug 撑爆存储。
    message: str = Field(default="", max_length=4096)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        # 以 UTF-8 JSON 序列化字节数为准：len(str(dict)) 既不是稳定
        # 序列化契约，也把中文按 1 字符计（UTF-8 实为 3 字节）。
        serialized = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(serialized) > _EVENT_PAYLOAD_MAX_BYTES:
            raise ValueError(
                f"event payload too large ({len(serialized)} > "
                f"{_EVENT_PAYLOAD_MAX_BYTES} bytes)")
        return value


class JobEventBatch(BaseModel):
    attempt_id: str = Field(max_length=128)
    events: list[JobEvent] = Field(min_length=1, max_length=200)


class CommandEvent(JobEvent):
    """Worker 命令过程日志事件。"""


class CommandEventBatch(BaseModel):
    events: list[CommandEvent] = Field(min_length=1, max_length=100)
