from starlette.responses import Response

from features.automation.artifacts import artifact_download_progress
from features.automation.models import TERMINAL_STATUSES as AUTOMATION_TERMINAL_STATUSES
from features.automation.models import AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.service import AutomationNotFoundError, AutomationService
from foundation.automation_port import AutomationWorkerStatus, configure_worker_status_provider


def get_worker_status() -> AutomationWorkerStatus:
    """Return scheduler state without exposing the worker module."""

    from features.automation.worker import get_worker_status as _status

    return _status()


def automation_ui_asset(ui_asset: str) -> Response:
    """Serve an automation page JS chunk (public surface for tests/tools)."""
    from features.automation.api import automation_ui_asset as _serve

    return _serve(ui_asset)


def get_automation_service() -> AutomationService:
    """Return the module-level AutomationService singleton.

    Public surface for cross-feature consumers (ops alert sweep); importing
    ``features.automation.api`` directly is an architecture violation.
    """
    from features.automation.api import automation_service

    return automation_service


def register_worker_status_port() -> None:
    """Wire the scheduler-status provider into ``foundation.automation_port``.

    Called by the composition root (``bootstrap.dependencies``) at startup;
    importing this package alone does not wire the port, so system health
    and metrics keep their documented degraded status.
    """
    configure_worker_status_provider(get_worker_status)


__all__ = [
    'AUTOMATION_TERMINAL_STATUSES',
    'AutomationNotFoundError',
    'AutomationOrchestrator',
    'AutomationRunCreateRequest',
    'AutomationService',
    'artifact_download_progress',
    'get_automation_service',
    'get_worker_status',
    'register_worker_status_port',
]
