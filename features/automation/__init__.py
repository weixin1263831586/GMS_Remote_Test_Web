from features.automation.models import AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.service import AutomationService


def get_worker_status():
    """Return scheduler state without exposing the worker module."""

    from features.automation.worker import get_worker_status as _status

    return _status()


__all__ = [
    'AutomationOrchestrator',
    'AutomationRunCreateRequest',
    'AutomationService',
    'get_worker_status',
]
