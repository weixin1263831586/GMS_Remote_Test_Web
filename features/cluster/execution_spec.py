"""ExecutionSpec → argv builder for structured Cluster Job test dispatch.

The validation/argv logic lives in ``foundation.execution_spec`` so the
Worker Agent can rebuild argv from the spec without trusting the payload's
argv.  This module adapts ``ExecutionSpecError`` to FastAPI ``HTTPException``
and keeps the Controller-only default-argv helper.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from foundation.execution_spec import (
    ExecutionSpecError,
)
from foundation.execution_spec import (
    build_argv_from_spec as _build_argv_from_spec,
)
from foundation.execution_spec import (
    canonicalize_execution_spec as _canonicalize_execution_spec,
)


def canonicalize_execution_spec(
    spec: dict,
    *,
    suite_path: str,
    suite_type: str,
    devices: list[str],
    worker_id: str,
) -> dict:
    """Bind a client spec to the suite inventory and leased device request."""
    try:
        return _canonicalize_execution_spec(
            spec,
            suite_path=suite_path,
            suite_type=suite_type,
            devices=devices,
            worker_id=worker_id,
        )
    except ExecutionSpecError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def build_argv_from_spec(spec: dict) -> list[str]:
    """Build a run_GMS_Test_Auto.sh argv from a structured ExecutionSpec."""
    try:
        return _build_argv_from_spec(spec)
    except ExecutionSpecError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def build_default_argv(
    *,
    suite_path: str,
    suite_key: str,
    worker_id: str,
    local_worker_id: str,
    available_suites: list[dict],
) -> tuple[list[str], str, str]:
    """Build the harmless default command used when no test argv is supplied."""
    suite_type = ""
    if not suite_path:
        suites = [
            item
            for item in available_suites
            if item["suite_key"] == suite_key and item["available"]
        ]
        if not suites:
            raise HTTPException(409, "suite is not available on worker")
        suite_path = suites[0]["tools_path"]
        suite_key = suites[0]["suite_key"]
        suite_type = suites[0]["suite_type"].lower()
    executable = (
        str(Path(suite_path) / f"{suite_type}-tradefed") if suite_type else ""
    )
    if not executable and worker_id == local_worker_id:
        executable = next(
            (
                str(Path(suite_path) / name)
                for name in (
                    "cts-tradefed",
                    "gts-tradefed",
                    "vts-tradefed",
                    "sts-tradefed",
                )
                if (Path(suite_path) / name).exists()
            ),
            "",
        )
    if not executable:
        raise HTTPException(409, "suite executable not found")
    return [executable, "list", "devices"], suite_path, suite_key
