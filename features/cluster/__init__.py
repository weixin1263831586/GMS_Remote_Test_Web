"""Multi-host test cluster controller."""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request

from foundation import cluster_port
from foundation.error_model import ApiError, format_validation_errors

from .admission import worker_admission_state
from .api import configure_cluster, device_action, page_router, router
from .api import service as get_cluster_service
from .config import ClusterConfig
from .device_operations.aoa_playback import control_aoa_playback
from .firmware_recovery import (
    freeze_firmware_recovery,
    prepare_local_firmware_recovery,
    release_local_firmware_recovery,
    run_local_firmware_recovery,
    validate_local_firmware_recovery,
)
from .jobs.api import cancel_job
from .jobs.execution_spec import build_argv_from_spec
from .jobs.requests import request_fingerprint
from .local_bridge import start_local_bridge, stop_local_bridge
from .models import ClusterDeviceAction
from .report_index import index_cluster_report
from .repository import ClusterRepository
from .service import ClusterService
from .worker_auth import authenticate_worker, worker_tokens


def run_artifact_retention():
    from .artifact_retention import run_enabled_retention

    return run_enabled_retention(get_cluster_service().repository)


async def run_worker_command(
    worker_id: str, command_type: str, payload: dict[str, Any], timeout: float = 10,
) -> dict[str, Any]:
    """Execute through the configured Cluster command boundary."""
    from .api import _run_worker_command

    return await _run_worker_command(worker_id, command_type, payload, timeout=timeout)


def require_cluster_enabled(remote: bool = False) -> None:
    """Apply the Controller's runtime cluster-mode gate."""
    from .api import _require_cluster_enabled

    _require_cluster_enabled(remote)


def require_worker_session(worker_id: str, session_id: str = '', generation: int = 0) -> None:
    """Validate a Worker session through the command API boundary."""
    from .commands_api import _require_worker_session

    _require_worker_session(worker_id, session_id, generation)


def _port_aoa_playback_control(
    control_id: str, worker_id: str, owner_id: str, action: str,
) -> Awaitable[dict[str, Any]]:
    return control_aoa_playback(control_id, worker_id, owner_id, action)


def _port_cluster_service() -> ClusterService:
    return get_cluster_service()


def _port_cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    return cancel_job(job_id, request)


def _port_worker_tokens() -> dict[str, str]:
    # Resolve the package-level name so tests patching
    # features.cluster.worker_tokens are honored.
    return worker_tokens()


def _port_authenticate_worker(worker_id: str, authorization: str | None) -> None:
    authenticate_worker(worker_id, authorization)


async def _port_device_action_request(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        body = ClusterDeviceAction(**payload)
    except ValidationError as exc:
        raise ApiError.invalid_semantics(format_validation_errors(exc.errors())) from exc
    return await device_action(body, request)


def _port_prepare_local_firmware_recovery(
    request: Request, client_id: str, devices: list[str], profile_id: str, boot_wait_seconds: int | str = 60,
) -> cluster_port.PreparedFirmwareRecovery | None:
    return prepare_local_firmware_recovery(request, client_id, devices, profile_id, boot_wait_seconds=boot_wait_seconds)


def _port_run_local_firmware_recovery(
    prepared: cluster_port.PreparedFirmwareRecovery, on_event: Callable[[dict[str, Any]], None] | None = None,
) -> Awaitable[cluster_port.FirmwareRecoveryResult]:
    return run_local_firmware_recovery(prepared, on_event=on_event)


def _port_validate_local_firmware_recovery(prepared: cluster_port.PreparedFirmwareRecovery | None) -> None:
    validate_local_firmware_recovery(prepared)


def _port_release_local_firmware_recovery(prepared: cluster_port.PreparedFirmwareRecovery | None) -> None:
    release_local_firmware_recovery(prepared)


# Register this feature with the foundation access port so consumers that
# must not import cluster (devices / reports / system / test_execution /
# users) share one seam. The wrappers resolve names per call, honoring test
# patches and service reconfiguration.
def register_cluster_port() -> None:
    """Wire this feature's capabilities into ``foundation.cluster_port``.

    Called by the composition root (``bootstrap.dependencies``) at startup;
    importing this package alone does not wire the port, so single-module
    consumers keep their documented single-host fallback.
    """
    cluster_port.configure_cluster_access(
        device_action_request=_port_device_action_request,
        aoa_playback_control=_port_aoa_playback_control,
        prepare_local_firmware_recovery=_port_prepare_local_firmware_recovery,
        run_local_firmware_recovery=_port_run_local_firmware_recovery,
        validate_local_firmware_recovery=_port_validate_local_firmware_recovery,
        release_local_firmware_recovery=_port_release_local_firmware_recovery,
        service_provider=_port_cluster_service,
        cancel_job=_port_cancel_job,
        worker_tokens=_port_worker_tokens,
        run_worker_command=run_worker_command,
        require_worker_session=require_worker_session,
        require_cluster_enabled=require_cluster_enabled,
        authenticate_worker=_port_authenticate_worker,
    )


__all__ = [
    "ClusterConfig",
    "ClusterDeviceAction",
    "ClusterRepository",
    "ClusterService",
    "authenticate_worker",
    "build_argv_from_spec",
    "cancel_job",
    "configure_cluster",
    "control_aoa_playback",
    "device_action",
    "freeze_firmware_recovery",
    "get_cluster_service",
    "index_cluster_report",
    "page_router",
    "prepare_local_firmware_recovery",
    "register_cluster_port",
    "release_local_firmware_recovery",
    "request_fingerprint",
    "require_cluster_enabled",
    "require_worker_session",
    "router",
    "run_artifact_retention",
    "run_local_firmware_recovery",
    "run_worker_command",
    "start_local_bridge",
    "stop_local_bridge",
    "validate_local_firmware_recovery",
    "worker_admission_state",
    "worker_tokens",
]
