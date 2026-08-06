"""Multi-host test cluster controller."""

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
    "require_cluster_enabled",
    "require_worker_session",
    "router",
    "run_worker_command",
    "start_local_bridge",
    "stop_local_bridge",
    "worker_tokens",
]
