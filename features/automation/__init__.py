from features.automation.models import AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.service import AutomationService
from foundation.automation_port import configure_worker_status_provider


def get_worker_status():
    """Return scheduler state without exposing the worker module."""

    from features.automation.worker import get_worker_status as _status

    return _status()


def _port_worker_status():
    return get_worker_status()


def register_worker_status_port() -> None:
    """Wire the scheduler-status provider into ``foundation.automation_port``.

    Called by the composition root (``bootstrap.dependencies``) at startup;
    importing this package alone does not wire the port, so system health
    and metrics keep their documented degraded status.
    """
    configure_worker_status_provider(_port_worker_status)


__all__ = [
    'AutomationOrchestrator',
    'AutomationRunCreateRequest',
    'AutomationService',
    'get_worker_status',
    'register_worker_status_port',
]
