"""Stable transport execution contract shared by Controller and Worker.

The web application deliberately treats ADB Proxy and USB/IP implementations as
replaceable executors.  A future native binary can implement this JSON contract
without importing FastAPI, the Controller runtime, or Worker persistence code.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


TRANSPORT_SCHEMA_VERSION = 1


@dataclass(slots=True)
class TransportOperationError(RuntimeError):
    code: str
    message: str
    retryable: bool = False
    remediation: str = ""
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.code,
            "error": self.message,
            "retryable": self.retryable,
            "remediation": self.remediation,
            "error_details": self.details or {},
        }


def transport_result(
    transport: str,
    result: dict[str, Any] | None = None,
    *,
    transport_state: str,
    protocol_state: str = "unknown",
    readiness: str = "transport_ready",
    generation: int = 0,
) -> dict[str, Any]:
    """Add the versioned transport envelope while preserving legacy fields."""
    return {
        **(result or {}),
        "transport_contract_version": TRANSPORT_SCHEMA_VERSION,
        "transport": transport,
        "transport_state": transport_state,
        "protocol_state": protocol_state,
        "readiness": readiness,
        "generation": max(0, int(generation or 0)),
    }


def execute_external_transport(
    command: str,
    *,
    transport: str,
    action: str,
    payload: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """Execute a replaceable transport binary using JSON stdin/stdout.

    Command-line arguments never contain grants, pair codes, passwords, or
    device selections.  The executor receives exactly one JSON request on
    stdin and must write exactly one JSON object to stdout.
    """
    argv = shlex.split(str(command or "").strip())
    if not argv:
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_NOT_CONFIGURED",
            f"{transport} executor command is empty",
        )
    executable = argv[0]
    if os.path.sep in executable:
        available = os.path.isfile(executable) and os.access(executable, os.X_OK)
    else:
        available = shutil.which(executable) is not None
    if not available:
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_NOT_FOUND",
            f"{transport} executor is not executable: {executable}",
            remediation="Install the transport executor or clear its environment override.",
        )
    request = {
        "schema_version": TRANSPORT_SCHEMA_VERSION,
        "transport": transport,
        "action": action,
        "payload": payload,
    }
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(request, separators=(",", ":")),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_TIMEOUT",
            f"{transport} executor timed out after {timeout}s",
            retryable=True,
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-2000:]
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_FAILED",
            f"{transport} executor exited with code {completed.returncode}",
            retryable=True,
            details={"stderr": detail} if detail else {},
        )
    try:
        response = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_INVALID_RESPONSE",
            f"{transport} executor returned invalid JSON",
        ) from exc
    if not isinstance(response, dict):
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_INVALID_RESPONSE",
            f"{transport} executor response must be a JSON object",
        )
    if int(response.get("schema_version") or 0) != TRANSPORT_SCHEMA_VERSION:
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_VERSION_MISMATCH",
            f"{transport} executor contract version is incompatible",
        )
    if response.get("success") is False:
        error = response.get("error") or {}
        if not isinstance(error, dict):
            error = {"message": str(error)}
        raise TransportOperationError(
            str(error.get("code") or "TRANSPORT_EXECUTOR_OPERATION_FAILED"),
            str(error.get("message") or f"{transport} operation failed"),
            bool(error.get("retryable")),
            str(error.get("remediation") or ""),
            error.get("details") if isinstance(error.get("details"), dict) else {},
        )
    data = response.get("data") or {}
    if not isinstance(data, dict):
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_INVALID_RESPONSE",
            f"{transport} executor data must be a JSON object",
        )
    return transport_result(
        transport,
        data,
        transport_state=str(response.get("transport_state") or "unknown"),
        protocol_state=str(response.get("protocol_state") or "unknown"),
        readiness=str(response.get("readiness") or "transport_ready"),
        generation=int(response.get("generation") or payload.get("generation") or 0),
    )


def execute_transport(
    env_name: str,
    *,
    transport: str,
    action: str,
    payload: dict[str, Any],
    timeout: int,
    builtin: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Use a configured binary executor, otherwise the built-in adapter."""
    command = os.getenv(env_name, "").strip()
    if command:
        return execute_external_transport(
            command,
            transport=transport,
            action=action,
            payload=payload,
            timeout=timeout,
        )
    return builtin()
