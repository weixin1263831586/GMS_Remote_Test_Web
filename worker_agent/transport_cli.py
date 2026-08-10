"""Reference JSON transport executor entry point.

This module is intentionally free of Controller/FastAPI imports and can be
packaged as a standalone binary while keeping the wire contract unchanged.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from foundation.transport_contract import (
    TRANSPORT_SCHEMA_VERSION,
    TransportOperationError,
)


def _response(result: dict[str, Any]) -> dict[str, Any]:
    envelope_keys = {
        "transport_contract_version", "transport", "transport_state",
        "protocol_state", "readiness", "generation",
    }
    return {
        "schema_version": TRANSPORT_SCHEMA_VERSION,
        "success": True,
        "transport_state": result.get("transport_state", "unknown"),
        "protocol_state": result.get("protocol_state", "unknown"),
        "readiness": result.get("readiness", "transport_ready"),
        "generation": int(result.get("generation") or 0),
        "data": {
            key: value for key, value in result.items()
            if key not in envelope_keys
        },
    }


def execute_request(request: dict[str, Any]) -> dict[str, Any]:
    if int(request.get("schema_version") or 0) != TRANSPORT_SCHEMA_VERSION:
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_VERSION_MISMATCH",
            "unsupported transport contract version",
        )
    transport = str(request.get("transport") or "")
    action = str(request.get("action") or "")
    payload = request.get("payload") or {}
    if not isinstance(payload, dict):
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_INVALID_REQUEST",
            "payload must be a JSON object",
        )
    if transport == "adb_proxy":
        from .adb_proxy import _execute_adb_proxy_builtin

        result = _execute_adb_proxy_builtin(
            action,
            payload,
            pair_code=str(payload.get("pair_code") or ""),
        )
        return _response(result)
    if transport == "usbip":
        raise TransportOperationError(
            "TRANSPORT_EXECUTOR_INVALID_REQUEST",
            "USB/IP is Rust-only; invoke gms-usbip-control directly",
        )
    raise TransportOperationError(
        "TRANSPORT_EXECUTOR_INVALID_REQUEST",
        f"unsupported transport: {transport}",
    )


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise TransportOperationError(
                "TRANSPORT_EXECUTOR_INVALID_REQUEST",
                "request must be a JSON object",
            )
        response = execute_request(request)
    except TransportOperationError as exc:
        response = {
            "schema_version": TRANSPORT_SCHEMA_VERSION,
            "success": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "remediation": exc.remediation,
                "details": exc.details or {},
            },
        }
    except Exception as exc:
        response = {
            "schema_version": TRANSPORT_SCHEMA_VERSION,
            "success": False,
            "error": {
                "code": "TRANSPORT_EXECUTOR_OPERATION_FAILED",
                "message": str(exc),
                "retryable": False,
                "remediation": "",
                "details": {},
            },
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
