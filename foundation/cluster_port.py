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


def configure_cluster_access(
    *,
    service_provider: ClusterServiceProvider | None = _UNSET,
    cancel_job: LateBoundCallable | None = _UNSET,
    worker_tokens: WorkerTokens | None = _UNSET,
    run_worker_command: LateBoundCallable | None = _UNSET,
    require_worker_session: LateBoundCallable | None = _UNSET,
    require_cluster_enabled: LateBoundCallable | None = _UNSET,
    authenticate_worker: LateBoundCallable | None = _UNSET,
) -> None:
    """Register or clear the cluster access callables.

    Omitted arguments keep their current registration; passing ``None``
    explicitly clears that callable (its accessor then raises
    ``RuntimeError`` so callers fall back to single-host behavior).
    """
    global _cluster_service_provider, _cancel_job, _worker_tokens
    global _run_worker_command, _require_worker_session
    global _require_cluster_enabled, _authenticate_worker
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


def get_cluster_service() -> Any:
    """Return the cluster service, or raise when unavailable."""
    if _cluster_service_provider is None:
        raise RuntimeError("cluster service is not configured")
    return _cluster_service_provider()


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
