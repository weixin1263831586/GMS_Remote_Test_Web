from contextlib import AbstractContextManager
from typing import Any

from features.build.executor import BuildExecutionError
from features.build.models import JOB_FAILED, JOB_QUEUED, JOB_RUNNING
from features.build.repository import BuildStore
from features.build.service import BuildService


def get_build_service() -> BuildService:
    """Return the currently configured service, not the import-time default."""
    from features.build import api

    return api.build_service


def open_build_artifact(
    job_id: str,
    path: str,
    *,
    automation_run_id: str | None = None,
) -> AbstractContextManager[tuple[Any, int]]:
    """Cross-feature surface: stream a completed job's artifact.

    Yields ``(binary stream, size)``; only artifacts reported by the job are
    readable, and ``automation_run_id`` enforces ATS ownership when set.
    """
    return get_build_service().open_build_artifact(
        job_id, path, automation_run_id=automation_run_id
    )


__all__ = [
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "BuildExecutionError",
    "BuildService",
    "BuildStore",
    "get_build_service",
    "open_build_artifact",
]
