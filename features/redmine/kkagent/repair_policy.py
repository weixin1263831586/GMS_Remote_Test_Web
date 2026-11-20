"""Decode the declared file path of an opaque SDK result_id.

This is a consistency check; the SDK backend verifies the token HMAC.
"""
from __future__ import annotations

import base64
import json
from typing import Any


def signed_source_path(result_id: str) -> str:
    if not result_id.startswith("src1_"):
        return ""
    encoded, separator, signature = result_id[5:].rpartition("_")
    if not separator or not signature or len(encoded) > 1600:
        return ""
    try:
        value = json.loads(base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8"))
        return str(value.get("path") or "") if isinstance(value, dict) else ""
    except (ValueError, TypeError, UnicodeError):
        return ""

def native_policy_kwargs(entry: dict[str, Any], targets: list[str]) -> dict[str, Any]:
    env = dict(entry.get("_kkagent_config_env") or {})
    env["GMS_MCP_REPAIR_POLICY"] = json.dumps(
        {"mode": "targeted" if targets else "deny", "targets": targets},
        ensure_ascii=False,
    )
    return {"config_env": env}


def approved_repair_call(call: Any, targets: list[str]) -> bool:
    """Post-execution defense in depth; MCP pre-dispatch guard is authoritative."""
    if call.tool_name == "gms_rt_sdk_search":
        path = str(call.tool_input.get("path") or "")
        scope = str(call.tool_input.get("repo_scope") or "").strip("/")
        return path in targets and bool(scope) and path.startswith(scope + "/")
    if call.tool_name == "gms_rt_sdk_read":
        path = signed_source_path(str(call.tool_input.get("result_id") or ""))
        return (path or str(call.tool_input.get("path") or "")) in targets
    return False
