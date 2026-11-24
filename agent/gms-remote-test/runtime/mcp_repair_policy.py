"""Bounded, pre-dispatch read-only SDK tool policy for claim review.

The Controller sets GMS_MCP_REPAIR_POLICY on the kkagent subprocess. A malformed
nonempty policy fails closed; absence retains normal MCP tool availability.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any


def current_policy() -> dict[str, Any] | None:
    raw = os.environ.get("GMS_MCP_REPAIR_POLICY", "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("mode") not in ("deny", "targeted", "targeted_exact"):
            return {"mode": "deny", "targets": []}
        targets = value.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 16:
            return {"mode": "deny", "targets": []}
        for target in targets:
            if (not isinstance(target, str) or len(target) > 180 or
                    len(target.split("/")) < 3 or
                    target.split("/")[0] not in {
                        "frameworks", "packages", "system", "hardware", "vendor",
                        "device", "kernel",
                    } or
                    any(part in ("", ".", "..", ".git") for part in target.split("/"))):
                return {"mode": "deny", "targets": []}
        mode = "deny" if value["mode"] == "deny" else "targeted_exact"
        return {"mode": mode, "targets": targets}
    except (TypeError, ValueError):
        return {"mode": "deny", "targets": []}


def tool_visible(name: str, policy: dict[str, Any] | None) -> bool:
    if policy is None:
        return True
    return policy["mode"] == "targeted_exact" and name in {
        "gms_rt_sdk_search", "gms_rt_sdk_read",
    }


def signed_path(result_id: str) -> str:
    """Extract declared path only; backend still verifies signed token HMAC."""
    if not result_id.startswith("src1_"):
        return ""
    encoded, sep, signature = result_id[5:].rpartition("_")
    if not sep or not signature or len(encoded) > 1600:
        return ""
    try:
        value = json.loads(base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8"))
        return str(value.get("path") or "") if isinstance(value, dict) else ""
    except (ValueError, UnicodeError, TypeError):
        return ""


def authorize(
    name: str, arguments: dict[str, Any], policy: dict[str, Any] | None,
    issued_searches: set[str], issued_reads: set[str],
) -> str | None:
    """Check scope and one-call budget before any CLI/SSH request is sent."""
    if policy is None:
        return None
    targets = policy.get("targets", [])
    if policy.get("mode") != "targeted_exact":
        return "report-only evidence review does not allow MCP tool calls"
    if not isinstance(arguments, dict):
        return "tool arguments must be an object"
    if name == "gms_rt_sdk_search":
        scope = str(arguments.get("repo_scope") or "").strip("/")
        path = str(arguments.get("path") or "")
        if not (path in targets and scope and path.startswith(scope + "/")):
            return "source repair requires exact approved path and repo_scope"
        if path in issued_searches:
            return "source repair already attempted this target"
        issued_searches.add(path)
        return None
    if name == "gms_rt_sdk_read":
        token = str(arguments.get("result_id") or "")
        path = signed_path(token)
        if path not in targets:
            return "source read needs a signed result_id for an approved file"
        if path in issued_reads:
            return "source repair already read this target"
        issued_reads.add(path)
        return None
    return "tool is outside the Controller-approved repair policy"
