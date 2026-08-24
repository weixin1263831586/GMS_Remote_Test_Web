"""Multi-host test cluster controller."""

from foundation import cluster_port

from .api import configure_cluster, device_action, page_router, router
from .api import service as get_cluster_service
from .config import ClusterConfig
from .job_control_api import cancel_job
from .local_bridge import start_local_bridge, stop_local_bridge
from .models import ClusterDeviceAction
from .report_index import index_cluster_report
from .repository import ClusterRepository
from .service import ClusterService
from .worker_auth import authenticate_worker, worker_tokens


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
    "cancel_job",
    "configure_cluster",
    "device_action",
    "get_cluster_service",
    "index_cluster_report",
    "page_router",
    "register_cluster_port",
    "require_cluster_enabled",
    "require_worker_session",
    "router",
    "run_worker_command",
    "start_local_bridge",
    "stop_local_bridge",
    "worker_tokens",
]
