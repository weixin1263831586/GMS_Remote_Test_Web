"""Multi-host test cluster controller."""


from pydantic import ValidationError

from foundation import cluster_port
from foundation.error_model import ApiError, format_validation_errors

from .admission import worker_admission_state
from .aoa_playback import control_aoa_playback
from .api import configure_cluster, device_action, page_router, router
from .api import service as get_cluster_service
from .config import ClusterConfig
from .execution_spec import build_argv_from_spec
from .firmware_recovery import (
    freeze_firmware_recovery,
    prepare_local_firmware_recovery,
    release_local_firmware_recovery,
    run_local_firmware_recovery,
    validate_local_firmware_recovery,
)
from .job_control_api import cancel_job
from .job_requests import request_fingerprint
from .local_bridge import start_local_bridge, stop_local_bridge
from .models import ClusterDeviceAction
from .report_index import index_cluster_report
from .repository import ClusterRepository
from .service import ClusterService
from .state_machine import ACTIVE_CLUSTER_JOB_STATUSES
from .worker_auth import authenticate_worker, worker_tokens


def run_artifact_retention():
    from .artifact_retention import run_enabled_retention

    return run_enabled_retention(get_cluster_service().repository)


async def run_worker_command(*args, **kwargs):
    """Execute through the configured Cluster command boundary."""
    from .api import _run_worker_command

    return await _run_worker_command(*args, **kwargs)


def require_cluster_enabled(*args, **kwargs):
    """Apply the Controller's runtime cluster-mode gate."""
    from .api import _require_cluster_enabled

    return _require_cluster_enabled(*args, **kwargs)


def require_worker_session(*args, **kwargs):
    """Validate a Worker session through the command API boundary."""
    from .commands_api import _require_worker_session

    return _require_worker_session(*args, **kwargs)


def _port_aoa_playback_control(*args, **kwargs):
    return control_aoa_playback(*args, **kwargs)


def _port_cluster_service():
    return get_cluster_service()


def _port_cancel_job(*args, **kwargs):
    return cancel_job(*args, **kwargs)


def _port_worker_tokens():
    # Resolve the package-level name so tests patching
    # features.cluster.worker_tokens are honored.
    return worker_tokens()


def _port_authenticate_worker(*args, **kwargs):
    return authenticate_worker(*args, **kwargs)


async def _port_device_action_request(request, payload):
    try:
        body = ClusterDeviceAction(**payload)
    except ValidationError as exc:
        raise ApiError.invalid_semantics(format_validation_errors(exc.errors())) from exc
    return await device_action(body, request)


def _port_prepare_local_firmware_recovery(*args, **kwargs):
    return prepare_local_firmware_recovery(*args, **kwargs)


def _port_run_local_firmware_recovery(*args, **kwargs):
    return run_local_firmware_recovery(*args, **kwargs)


def _port_validate_local_firmware_recovery(*args, **kwargs):
    return validate_local_firmware_recovery(*args, **kwargs)


def _port_release_local_firmware_recovery(*args, **kwargs):
    return release_local_firmware_recovery(*args, **kwargs)


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
    "ACTIVE_CLUSTER_JOB_STATUSES",
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
