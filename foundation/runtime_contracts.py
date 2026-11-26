"""Callable contracts shared by Controller feature composition."""

from __future__ import annotations

from asyncio import Lock, Task
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, Protocol

from paramiko import SFTPClient, SSHClient
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket

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


class ControllerStateContract(Protocol):
    """Mutable Controller process state shared by configured features."""

    test_logs: dict[str, list[Any]]
    test_logs_lock: AbstractContextManager[bool]
    last_saved_log_file: dict[str, str]
    device_cache: dict[str, Any]
    device_cache_lock: AbstractContextManager[bool]
    websocket_connections: dict[str, WebSocket | set[WebSocket]]
    websocket_connections_lock: AbstractContextManager[bool]
    firmware_upload_progress: dict[str, Any]
    firmware_upload_progress_lock: AbstractContextManager[bool]
    usbip_states: dict[str, dict[str, Any]]
    usbip_states_lock: AbstractContextManager[bool]
    usbip_devices_source: dict[str, dict[str, Any]]
    usbip_devices_source_lock: AbstractContextManager[bool]
    user_states: dict[str, dict[str, Any]]
    user_states_lock: AbstractContextManager[bool]
    apk_analysis_tasks: dict[str, dict[str, Any]]
    apk_analysis_tasks_lock: AbstractContextManager[bool]
    apk_upload_locks: dict[str, Lock]
    apk_upload_locks_lock: AbstractContextManager[bool]
    background_tasks: set[Task[Any]]

    def device_ssh_pool_get(
        self, pool_key: str, config: dict[str, Any], pool_size: int = 3,
    ) -> SSHClient | None: ...

    def device_ssh_pool_return(self, pool_key: str, ssh: SSHClient) -> None: ...


class SSHManagerContract(Protocol):
    def create_connection(
        self, config: dict[str, Any], *, raise_on_error: bool = False,
    ) -> SSHClient | None: ...

    def get_connection(self, config: dict[str, Any]) -> SSHClient | None: ...

    def return_connection(self, ssh: SSHClient) -> None: ...

    def execute_command(
        self, ssh: SSHClient, command: str, timeout: int = 30, get_pty: bool = False,
    ) -> CommandResult: ...

    def check_sshd_installed(self, ssh: SSHClient) -> tuple[bool, str]: ...

    def install_sshd(self, ssh: SSHClient, config: dict[str, Any]) -> dict[str, Any]: ...

    def connection(self, config: dict[str, Any]) -> AbstractContextManager[SSHClient]: ...

    def optional_connection(self, config: dict[str, Any]) -> AbstractContextManager[SSHClient | None]: ...

    def async_optional_connection(
        self, config: dict[str, Any],
    ) -> AbstractAsyncContextManager[SSHClient | None]: ...

    def optimize_sftp_performance(self, sftp: SFTPClient) -> None: ...


class SSHAsyncManagerContract(Protocol):
    async def execute_command_simple(
        self, host: str, username: str, password: str, command: str, timeout: int = 30,
    ) -> CommandResult: ...


class ClientIdManager(Protocol):
    def get_client_id(self, client_ip: str, username: str = "unknown") -> str: ...


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
