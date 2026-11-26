"""Callable contracts shared by Controller feature composition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from foundation.command_result import CommandResult


ClientIdResolver = Callable[[Request], str]
ClientIpResolver = Callable[[Request], str]
WebSocketSender = Callable[[str, dict[str, Any]], Awaitable[None]]
ApiHelpProvider = Callable[[bool, str, str], Response | None]
WindowsUsbipProbe = Callable[[str], Awaitable[dict[str, Any]]]
TailscaleHostResolver = Callable[[Request, str], tuple[str | None, str | None]]
UserStateProvider = Callable[[str], dict[str, Any]]
ApkTaskCreator = Callable[[str, str, str, str], None]
ApkFilenameNormalizer = Callable[[str | None], str]
FileCleanup = Callable[[list[str]], None]
DeviceLockRelease = Callable[[str, list[str]], Awaitable[None]]


class NotificationStore(Protocol):
    def __call__(
        self,
        client_id: str,
        title: str,
        message: str = "",
        level: str = "info",
        category: str = "system",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class LocalShellRunner(Protocol):
    def __call__(self, command: str, timeout: int = 30) -> CommandResult: ...


class FirmwareDeviceLocker(Protocol):
    def __call__(
        self,
        *,
        client_id: str,
        username: str,
        devices: list[str],
        error_prefix: str = "Devices occupied",
    ) -> Awaitable[tuple[list[str], JSONResponse | None]]: ...


class SafePathJoin(Protocol):
    def __call__(self, base_dir: str, *parts: str) -> str: ...


class TestStartRequest(BaseModel):
    # 空值表示部署配置中的本地 Worker。
    worker_id: str = ""
    test_type: str = ""
    test_module: str = ""
    test_case: str = ""
    retry_dir: str = ""
    test_suite: str = ""
    local_server: str = ""
    devices: list[str] = Field(default_factory=list)
    client_id: str = "test_client"
    automation_run_id: str = ""
    device_reservation_id: str = ""
    build_id: str = ""
    build_artifact_id: str = ""
    gerrit_change_id: str = ""
    gerrit_patchset: str = ""
    redmine_issue_id: str = ""
    shard_count: int = Field(default=0, ge=0, le=32, strict=True)
    preparations: list[str] = Field(default_factory=list, max_length=8)
    queue_timeout_seconds: int = Field(default=0, ge=0, le=604800, strict=True)
    execution_timeout_seconds: int = Field(default=0, ge=0, le=604800, strict=True)
    output_idle_timeout_seconds: int = Field(default=0, ge=0, le=86400, strict=True)


class ClusterTestStarter(Protocol):
    def __call__(self, request: TestStartRequest, client_id: str) -> Response: ...
