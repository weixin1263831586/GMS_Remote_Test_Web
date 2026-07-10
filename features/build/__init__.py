from features.build.models import JOB_FAILED, JOB_QUEUED, JOB_RUNNING
from features.build.repository import BuildStore
from features.build.service import BuildService


def get_build_service() -> BuildService:
    """Return the currently configured service, not the import-time default."""
    from features.build import api

    return api.build_service


__all__ = [
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "BuildService",
    "BuildStore",
    "get_build_service",
]
