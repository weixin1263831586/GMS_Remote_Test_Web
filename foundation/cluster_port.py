"""Access port for the cluster service.

Features that need cluster capabilities (durable jobs, worker inventory)
reach them through this port instead of importing ``features.cluster``.
The composition root registers a late-bound provider; when nothing is
registered the accessors raise ``RuntimeError`` so callers fall back to
single-host behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


ClusterServiceProvider = Callable[[], Any]
LateBoundCallable = Callable[..., Any]
WorkerTokens = Callable[[], dict[str, str]]

# Single source of truth for the Controller host's Worker ID. The literal
# must appear only here (the default), in fixtures/examples/tests/docs, and
# as a last-resort fallback in client scripts — never in business logic
# comparisons. ``features.cluster.config`` derives its default from it.
DEFAULT_LOCAL_WORKER_ID = "ats-worker-controller"

# Default for ``configure_cluster_access`` arguments: omitted means "keep the
# current registration", while an explicit ``None`` clears it.
_UNSET: Any = object()

_cluster_service_provider: ClusterServiceProvider | None = None
_cancel_job: LateBoundCallable | None = None
_worker_tokens: WorkerTokens | None = None
_run_worker_command: LateBoundCallable | None = None
_require_worker_session: LateBoundCallable | None = None
_require_cluster_enabled: LateBoundCallable | None = None
_authenticate_worker: LateBoundCallable | None = None
_device_action_request: LateBoundCallable | None = None
_aoa_playback_control: LateBoundCallable | None = None
_prepare_local_firmware_recovery: LateBoundCallable | None = None
_run_local_firmware_recovery: LateBoundCallable | None = None
_validate_local_firmware_recovery: LateBoundCallable | None = None
_release_local_firmware_recovery: LateBoundCallable | None = None


def configure_cluster_access(
    *,
    service_provider: ClusterServiceProvider | None = _UNSET,
    cancel_job: LateBoundCallable | None = _UNSET,
    worker_tokens: WorkerTokens | None = _UNSET,
    run_worker_command: LateBoundCallable | None = _UNSET,
    require_worker_session: LateBoundCallable | None = _UNSET,
    require_cluster_enabled: LateBoundCallable | None = _UNSET,
    authenticate_worker: LateBoundCallable | None = _UNSET,
    device_action_request: LateBoundCallable | None = _UNSET,
    aoa_playback_control: LateBoundCallable | None = _UNSET,
    prepare_local_firmware_recovery: LateBoundCallable | None = _UNSET,
    run_local_firmware_recovery: LateBoundCallable | None = _UNSET,
    validate_local_firmware_recovery: LateBoundCallable | None = _UNSET,
    release_local_firmware_recovery: LateBoundCallable | None = _UNSET,
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


def control_aoa_playback(*args: Any, **kwargs: Any) -> Any:
    """Control an existing playback through the registered ownership boundary."""
    if _aoa_playback_control is None:
        raise RuntimeError("AOA playback control is not configured")
    return _aoa_playback_control(*args, **kwargs)


def submit_device_action(request: Any, payload: dict) -> Any:
    """Submit through the configured device ownership and execution boundary."""
    if _device_action_request is None:
        raise RuntimeError('cluster device actions are not configured')
    return _device_action_request(request, payload)


def get_cluster_service() -> Any:
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


def cancel_durable_job(*args: Any, **kwargs: Any) -> Any:
    """Cancel a durable cluster job via the registered callable."""
    if _cancel_job is None:
        raise RuntimeError("cluster job cancellation is not configured")
    return _cancel_job(*args, **kwargs)


def worker_tokens() -> dict[str, str]:
    """Return the worker→token map via the registered callable."""
    if _worker_tokens is None:
        raise RuntimeError("cluster worker tokens are not configured")
    return _worker_tokens()


def run_worker_command(*args: Any, **kwargs: Any) -> Any:
    """Execute a Worker command via the registered callable."""
    if _run_worker_command is None:
        raise RuntimeError("cluster worker commands are not configured")
    return _run_worker_command(*args, **kwargs)


def require_worker_session(*args: Any, **kwargs: Any) -> Any:
    """Validate a Worker session via the registered callable."""
    if _require_worker_session is None:
        raise RuntimeError("cluster worker sessions are not configured")
    return _require_worker_session(*args, **kwargs)


def require_cluster_enabled(*args: Any, **kwargs: Any) -> Any:
    """Apply the cluster-mode gate via the registered callable."""
    if _require_cluster_enabled is None:
        raise RuntimeError("cluster mode gating is not configured")
    return _require_cluster_enabled(*args, **kwargs)


def authenticate_worker(*args: Any, **kwargs: Any) -> Any:
    """Authenticate a Worker request via the registered callable."""
    if _authenticate_worker is None:
        raise RuntimeError("cluster worker authentication is not configured")
    return _authenticate_worker(*args, **kwargs)


def prepare_local_firmware_recovery(*args: Any, **kwargs: Any) -> Any:
    """Prepare a burn+AOA continuation via the registered cluster boundary."""
    if _prepare_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _prepare_local_firmware_recovery(*args, **kwargs)


def run_local_firmware_recovery(*args: Any, **kwargs: Any) -> Any:
    """Run a burn+AOA continuation via the registered cluster boundary."""
    if _run_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _run_local_firmware_recovery(*args, **kwargs)


def validate_local_firmware_recovery(*args: Any, **kwargs: Any) -> Any:
    """Validate staged recovery identity via the registered cluster boundary."""
    if _validate_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _validate_local_firmware_recovery(*args, **kwargs)


def release_local_firmware_recovery(*args: Any, **kwargs: Any) -> Any:
    """Release a finished recovery via the registered cluster boundary."""
    if _release_local_firmware_recovery is None:
        raise RuntimeError("local firmware recovery is not configured")
    return _release_local_firmware_recovery(*args, **kwargs)


# 持久化 Cluster Job 视角"仍在进行"的状态全集。定义在 foundation 侧
# 组合接缝上，test_execution/users 等 features 共同消费且互不依赖。
ACTIVE_JOB_STATUSES = frozenset({
    "created", "queued", "leasing", "assigned", "dispatching", "running",
    "stopping", "collecting", "worker_lost",
})
