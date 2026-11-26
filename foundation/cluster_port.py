"""Access port for the cluster service.

Features that need cluster capabilities (durable jobs, worker inventory)
reach them through this port instead of importing ``features.cluster``.
The composition root registers a late-bound provider; when nothing is
registered the accessors raise ``RuntimeError`` so callers fall back to
single-host behavior.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from starlette.requests import Request

from foundation.device_claims import DeviceClaimRegistry


class ClusterConfigContract(Protocol):
    @property
    def local_worker_id(self) -> str: ...

    @property
    def remote_dispatch_enabled(self) -> bool: ...


class ClusterRepositoryContract(Protocol):
    """Storage capabilities consumed through the cluster port."""

    @property
    def db_path(self) -> Path: ...

    @property
    def claims(self) -> DeviceClaimRegistry: ...

    def get_worker(self, worker_id: str) -> dict[str, Any] | None: ...
    def list_devices(self, worker_id: str = '') -> list[dict[str, Any]]: ...
    def refresh_worker_devices(self, worker_id: str, devices: list[dict[str, Any]]) -> None: ...
    def upsert_seen_device(self, worker_id: str, serial: str) -> None: ...
    def resolve_worker_device(self, worker_id: str, value: str) -> dict[str, Any] | None: ...
    def get_job(self, job_id: str) -> dict[str, Any] | None: ...
    def list_jobs(
        self, limit: int = 100, owner_id: str = '', include_active: bool = False,
    ) -> list[dict[str, Any]]: ...
    def list_events(self, job_id: str, after: int = -1, limit: int = 500) -> list[dict[str, Any]]: ...
    def get_reservation_by_source(self, source_id: str) -> dict[str, Any] | None: ...
    def acquire_device_operation_claim(
        self, worker_id: str, devices: list[str], *, owner_id: str,
        source_type: str, source_id: str, ttl_seconds: int = 3600, username: str = '',
    ) -> list[dict[str, Any]]: ...
    def claim_fencing_tokens(
        self, records: list[dict[str, Any]], attempt_id: str, *,
        device_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]: ...
    def force_release_local_aoa_operation(self, device_key: str, local_worker_id: str) -> tuple[bool, str]: ...
    def create_command(self, data: dict[str, Any], *, attach_to_job: bool = False) -> dict[str, Any]: ...
    def get_command(self, command_id: str) -> dict[str, Any] | None: ...
    def find_correlated_command(
        self, worker_id: str, command_type: str, key: str, value: str,
    ) -> dict[str, Any] | None: ...
    def create_transfer(
        self, worker_id: str, transfer_type: str = 'suite_export', owner_id: str = '',
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def get_transfer(self, transfer_id: str) -> dict[str, Any] | None: ...
    def update_transfer(self, transfer_id: str, **values: Any) -> dict[str, Any] | None: ...


class ClusterServiceContract(Protocol):
    @property
    def config(self) -> ClusterConfigContract: ...

    @property
    def repository(self) -> ClusterRepositoryContract: ...

    @property
    def effective_enabled(self) -> bool: ...

    def list_workers(self) -> list[dict[str, Any]]: ...
    def has_command_agent(self, worker_id: str) -> bool: ...


ClusterServiceProvider = Callable[[], ClusterServiceContract]
WorkerTokens = Callable[[], dict[str, str]]
CancelJob = Callable[[str, Request], dict[str, Any]]
AuthenticateWorker = Callable[[str, str | None], None]
DeviceActionRequest = Callable[[Request, dict[str, Any]], Awaitable[Any]]
AoaPlaybackControl = Callable[[str, str, str, str], Awaitable[dict[str, Any]]]
FirmwareRecoveryCheck = Callable[[dict[str, Any] | None], None]


class RunWorkerCommand(Protocol):
    def __call__(
        self, worker_id: str, command_type: str, payload: dict[str, Any], timeout: float = 10,
    ) -> Awaitable[dict[str, Any]]: ...


class RequireWorkerSession(Protocol):
    def __call__(self, worker_id: str, session_id: str = "", generation: int = 0) -> None: ...


class RequireClusterEnabled(Protocol):
    def __call__(self, remote: bool = False) -> None: ...


class PrepareFirmwareRecovery(Protocol):
    def __call__(
        self, request: Request, client_id: str, devices: list[str],
        profile_id: str, boot_wait_seconds: int = 60,
    ) -> dict[str, Any] | None: ...


class RunFirmwareRecovery(Protocol):
    def __call__(
        self, prepared: dict[str, Any], on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Awaitable[dict[str, Any]]: ...

# Single source of truth for the Controller host's Worker ID. The literal
# must appear only here (the default), in fixtures/examples/tests/docs, and
# as a last-resort fallback in client scripts — never in business logic
# comparisons. ``features.cluster.config`` derives its default from it.
DEFAULT_LOCAL_WORKER_ID = "ats-worker-controller"

# Default for ``configure_cluster_access`` arguments: omitted means "keep the
# current registration", while an explicit ``None`` clears it.
_UNSET: Any = object()

_cluster_service_provider: ClusterServiceProvider | None = None
_cancel_job: CancelJob | None = None
_worker_tokens: WorkerTokens | None = None
_run_worker_command: RunWorkerCommand | None = None
_require_worker_session: RequireWorkerSession | None = None
_require_cluster_enabled: RequireClusterEnabled | None = None
_authenticate_worker: AuthenticateWorker | None = None
_device_action_request: DeviceActionRequest | None = None
_aoa_playback_control: AoaPlaybackControl | None = None
_prepare_local_firmware_recovery: PrepareFirmwareRecovery | None = None
_run_local_firmware_recovery: RunFirmwareRecovery | None = None
_validate_local_firmware_recovery: FirmwareRecoveryCheck | None = None
_release_local_firmware_recovery: FirmwareRecoveryCheck | None = None


def configure_cluster_access(
    *,
    service_provider: ClusterServiceProvider | None = _UNSET,
    cancel_job: CancelJob | None = _UNSET,
    worker_tokens: WorkerTokens | None = _UNSET,
    run_worker_command: RunWorkerCommand | None = _UNSET,
    require_worker_session: RequireWorkerSession | None = _UNSET,
    require_cluster_enabled: RequireClusterEnabled | None = _UNSET,
    authenticate_worker: AuthenticateWorker | None = _UNSET,
    device_action_request: DeviceActionRequest | None = _UNSET,
    aoa_playback_control: AoaPlaybackControl | None = _UNSET,
    prepare_local_firmware_recovery: PrepareFirmwareRecovery | None = _UNSET,
    run_local_firmware_recovery: RunFirmwareRecovery | None = _UNSET,
    validate_local_firmware_recovery: FirmwareRecoveryCheck | None = _UNSET,
    release_local_firmware_recovery: FirmwareRecoveryCheck | None = _UNSET,
) -> None:
    """Register or clear the cluster access callables.

    Omitted arguments keep their current registration; passing ``None``
    explicitly clears that callable (its accessor then raises
    ``RuntimeError`` so callers fall back to single-host behavior).
    """
    global _cluster_service_provider, _cancel_job, _worker_tokens
    global _run_worker_command, _require_worker_session
    global _require_cluster_enabled, _authenticate_worker
    global _device_action_request, _aoa_playback_control
    global _prepare_local_firmware_recovery, _run_local_firmware_recovery
    global _validate_local_firmware_recovery, _release_local_firmware_recovery
    if service_provider is not _UNSET:
        _cluster_service_provider = service_provider
    if cancel_job is not _UNSET:
        _cancel_job = cancel_job
    if worker_tokens is not _UNSET:
        _worker_tokens = worker_tokens
    if run_worker_command is not _UNSET:
        _run_worker_command = run_worker_command
    if require_worker_session is not _UNSET:
        _require_worker_session = require_worker_session
    if require_cluster_enabled is not _UNSET:
        _require_cluster_enabled = require_cluster_enabled
    if authenticate_worker is not _UNSET:
        _authenticate_worker = authenticate_worker
    if device_action_request is not _UNSET:
        _device_action_request = device_action_request
    if aoa_playback_control is not _UNSET:
        _aoa_playback_control = aoa_playback_control
    if prepare_local_firmware_recovery is not _UNSET:
        _prepare_local_firmware_recovery = prepare_local_firmware_recovery
    if run_local_firmware_recovery is not _UNSET:
        _run_local_firmware_recovery = run_local_firmware_recovery
    if validate_local_firmware_recovery is not _UNSET:
        _validate_local_firmware_recovery = validate_local_firmware_recovery
    if release_local_firmware_recovery is not _UNSET:
        _release_local_firmware_recovery = release_local_firmware_recovery


def control_aoa_playback(control_id: str, worker_id: str, owner_id: str, action: str) -> Awaitable[dict[str, Any]]:
    """Control an existing playback through the registered ownership boundary."""
    if _aoa_playback_control is None:
        raise RuntimeError("AOA playback control is not configured")
    return _aoa_playback_control(control_id, worker_id, owner_id, action)


def submit_device_action(request: Request, payload: dict[str, Any]) -> Awaitable[Any]:
    """Submit through the configured device ownership and execution boundary."""
    if _device_action_request is None:
        raise RuntimeError('cluster device actions are not configured')
    return _device_action_request(request, payload)


def get_cluster_service() -> ClusterServiceContract:
    """Return the cluster service, or raise when unavailable."""
    if _cluster_service_provider is None:
        raise RuntimeError("cluster service is not configured")
    return _cluster_service_provider()


def get_local_worker_id() -> str:
    """Return the configured Worker ID of the Controller host.

    Business logic that needs "is this the local/Controller worker?" must
    compare against this accessor, never against a hardcoded ID: deployments
    may set ``local_worker_id`` in the cluster config, and a hardcoded
    comparison silently breaks ownership fallbacks for them. Falls back to
    ``DEFAULT_LOCAL_WORKER_ID`` when the cluster service is not configured
    (single-host mode) or the config carries an empty value.
    """
    try:
        service = get_cluster_service()
        worker_id = str(
            getattr(getattr(service, "config", None), "local_worker_id", "")
            or ""
        ).strip()
        return worker_id or DEFAULT_LOCAL_WORKER_ID
    except Exception:
        return DEFAULT_LOCAL_WORKER_ID


def cancel_durable_job(job_id: str, request: Request) -> dict[str, Any]:
    """Cancel a durable cluster job via the registered callable."""
    if _cancel_job is None:
        raise RuntimeError("cluster job cancellation is not configured")
    return _cancel_job(job_id, request)


def worker_tokens() -> dict[str, str]:
    """Return the worker→token map via the registered callable."""
    if _worker_tokens is None:
        raise RuntimeError("cluster worker tokens are not configured")
    return _worker_tokens()


def run_worker_command(worker_id: str, command_type: str, payload: dict[str, Any], timeout: float = 10) -> Awaitable[dict[str, Any]]:
    """Execute a Worker command via the registered callable."""
    if _run_worker_command is None:
        raise RuntimeError("cluster worker commands are not configured")
    return _run_worker_command(worker_id, command_type, payload, timeout=timeout)


def require_worker_session(worker_id: str, session_id: str = '', generation: int = 0) -> None:
    """Validate a Worker session via the registered callable."""
    if _require_worker_session is None:
        raise RuntimeError("cluster worker sessions are not configured")
    return _require_worker_session(worker_id, session_id, generation)


def require_cluster_enabled(remote: bool = False) -> None:
    """Apply the cluster-mode gate via the registered callable."""
    if _require_cluster_enabled is None:
        raise RuntimeError("cluster mode gating is not configured")
    return _require_cluster_enabled(remote)


def authenticate_worker(worker_id: str, authorization: str | None) -> None:
    """Authenticate a Worker request via the registered callable."""
    if _authenticate_worker is None:
        raise RuntimeError("cluster worker authentication is not configured")
    return _authenticate_worker(worker_id, authorization)


def prepare_local_firmware_recovery(request: Request, client_id: str, devices: list[str], profile_id: str, boot_wait_seconds: int = 60) -> dict[str, Any] | None:
    """Prepare a burn+AOA continuation via the registered cluster boundary."""
    if _prepare_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _prepare_local_firmware_recovery(request, client_id, devices, profile_id, boot_wait_seconds=boot_wait_seconds)


def run_local_firmware_recovery(prepared: dict[str, Any], on_event: Callable[[dict[str, Any]], None] | None = None) -> Awaitable[dict[str, Any]]:
    """Run a burn+AOA continuation via the registered cluster boundary."""
    if _run_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _run_local_firmware_recovery(prepared, on_event=on_event)


def validate_local_firmware_recovery(prepared: dict[str, Any] | None) -> None:
    """Validate staged recovery identity via the registered cluster boundary."""
    if _validate_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _validate_local_firmware_recovery(prepared)


def release_local_firmware_recovery(prepared: dict[str, Any] | None) -> None:
    """Release a finished recovery via the registered cluster boundary."""
    if _release_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _release_local_firmware_recovery(prepared)


# 持久化 Cluster Job 视角"仍在进行"的状态全集。定义在 foundation 侧
# 组合接缝上，test_execution/users 等 features 共同消费且互不依赖。
ACTIVE_JOB_STATUSES = frozenset({
    "created", "queued", "leasing", "assigned", "dispatching", "running",
    "stopping", "collecting", "worker_lost",
})
