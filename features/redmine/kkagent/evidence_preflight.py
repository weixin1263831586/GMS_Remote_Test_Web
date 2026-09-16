"""Deterministic, read-only evidence collection before a deep AI analysis.

The model must not be the sole owner of required evidence calls: a missing
argument or a transient Controller connection must not turn into a misleading
"device evidence failed" result.  This module invokes the documented CLI with
validated, fixed arguments and records the result as ordinary GMS tool trace
entries.  It never invokes a shell and retries only the documented network
exit code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from .auth_preflight import GMS_SELFCHECK_SCRIPT
from .process import child_env, terminate_process_tree
from .trace import ToolTrace


NETWORK_EXIT_CODE = 6
PREFLIGHT_TIMEOUT_SECONDS = 90.0
NETWORK_RETRY_DELAYS = (0.0, 0.5, 1.5)


def _gms_command(command: str, arguments: list[str]) -> list[str] | None:
    """Use the canonical installed runtime when available, never a shell."""
    if os.path.isfile(GMS_SELFCHECK_SCRIPT):
        return ["bash", GMS_SELFCHECK_SCRIPT, command, *arguments]
    if shutil.which(command):
        return [command, *arguments]
    return None


def _summary_trace(
    *, tool_name: str, tool_input: dict[str, Any], status: str,
    output: bytes = b"", error: str = "", evidence_issue_ids: list[int] | None = None,
    snapshot_ids: list[str] | None = None, failure_kind: str = "",
) -> ToolTrace:
    """Persist provenance without retaining untrusted Redmine/device output."""
    digest = hashlib.sha256(output).hexdigest() if output else ""
    preview = "controller preflight succeeded" if status == "succeeded" else (
        error[:200] or "controller preflight failed"
    )
    return ToolTrace(
        tool_call_id="preflight:" + tool_name + ":" + hashlib.sha256(
            json.dumps(tool_input, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        tool_name=tool_name,
        tool_input=tool_input,
        status="succeeded" if status == "succeeded" else "failed",
        output_sha256=digest,
        output_bytes=len(output),
        output_preview=preview,
        failure_kind=failure_kind,
        evidence_issue_ids=list(evidence_issue_ids or []),
        snapshot_ids=list(snapshot_ids or []),
    )


async def _run_readonly_command(
    command: list[str], env_extra: dict[str, str], *, timeout_seconds: float,
) -> tuple[int, bytes, str]:
    """Run one bounded, read-only CLI call and return its exit/output summary."""
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(env_extra),
            start_new_session=os.name == "posix",
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds,
        )
        return int(process.returncode or 0), stdout, stderr.decode(
            "utf-8", errors="replace"
        )[:500]
    except asyncio.TimeoutError:
        if process is not None:
            await terminate_process_tree(process)
        return NETWORK_EXIT_CODE, b"", "Controller evidence request timed out"
    except OSError as exc:
        return NETWORK_EXIT_CODE, b"", f"Controller evidence request unavailable: {exc}"
    except asyncio.CancelledError:
        if process is not None:
            cleanup = asyncio.create_task(terminate_process_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        raise


async def _collect(
    *, tool_name: str, arguments: list[str], tool_input: dict[str, Any],
    env_extra: dict[str, str], evidence_issue_ids: list[int] | None = None,
) -> tuple[ToolTrace, dict[str, Any]]:
    command = _gms_command(tool_name.replace("gms_rt_", "gms-rt-").replace("_", "-"), arguments)
    if command is None:
        return _summary_trace(
            tool_name=tool_name, tool_input=tool_input, status="failed",
            error="GMS evidence CLI is unavailable", evidence_issue_ids=evidence_issue_ids,
        ), {}
    output = b""
    error = ""
    exit_code = NETWORK_EXIT_CODE
    for _attempt, delay in enumerate(NETWORK_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        exit_code, output, error = await _run_readonly_command(
            command, env_extra, timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
        )
        if exit_code != NETWORK_EXIT_CODE:
            break
    try:
        payload = json.loads(output.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        payload = {}
    success = exit_code == 0 and (
        not isinstance(payload, dict)
        or payload.get("ok", payload.get("success", True)) is not False
    )
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    snapshot_id = str(data.get("snapshot_id") or "") if isinstance(data, dict) else ""
    trace = _summary_trace(
        tool_name=tool_name,
        tool_input=tool_input,
        status="succeeded" if success else "failed",
        output=output,
        error=error or f"GMS CLI exited with code {exit_code}",
        evidence_issue_ids=evidence_issue_ids,
        snapshot_ids=[snapshot_id] if snapshot_id else [],
        failure_kind=(
            "" if success else "service_unavailable"
            if exit_code == NETWORK_EXIT_CODE else "invalid_request"
            if exit_code == 2 else "device_unavailable"
            if tool_name == "gms_rt_devices_snapshot" else "operation_failed"
        ),
    )
    return trace, data if success and isinstance(data, dict) else {}


@dataclass
class EvidencePreflight:
    """Sanitized pre-analysis evidence and traces for one deep-analysis issue."""

    traces: list[ToolTrace] = field(default_factory=list)
    snapshot_id: str = ""
    device_status: str = "not_requested"

    def prompt_context(self) -> dict[str, str]:
        return {
            "snapshot_id": self.snapshot_id,
            "device_status": self.device_status,
        }


async def collect_deep_analysis_evidence(
    *, issue_id: int, device_serial: str, env_extra: dict[str, str],
) -> EvidencePreflight:
    """Collect the mandatory immutable evidence baseline for a single issue.

    Redmine fetch/journals/attachments and the optional device snapshot are all
    idempotent, read-only evidence operations.  A failed preflight is recorded
    but does not suppress the later analysis: the final report can accurately
    state which source was unavailable.
    """
    result = EvidencePreflight()
    fetched, data = await _collect(
        tool_name="gms_rt_redmine_issue_fetch",
        arguments=[str(issue_id), "--wait", "--json", "--non-interactive"],
        tool_input={"issue_id": issue_id}, env_extra=env_extra,
        evidence_issue_ids=[issue_id],
    )
    result.traces.append(fetched)
    result.snapshot_id = str(data.get("snapshot_id") or "")
    if result.snapshot_id:
        journals, _ = await _collect(
            tool_name="gms_rt_redmine_journals",
            arguments=[result.snapshot_id, "--json", "--non-interactive"],
            tool_input={"snapshot_id": result.snapshot_id}, env_extra=env_extra,
            evidence_issue_ids=[issue_id],
        )
        attachments, _ = await _collect(
            tool_name="gms_rt_redmine_attachments",
            arguments=[result.snapshot_id, "--json", "--non-interactive"],
            tool_input={"snapshot_id": result.snapshot_id}, env_extra=env_extra,
            evidence_issue_ids=[issue_id],
        )
        result.traces.extend([journals, attachments])
    if device_serial:
        device, _ = await _collect(
            tool_name="gms_rt_devices_snapshot",
            arguments=[device_serial, "--json", "--non-interactive"],
            tool_input={"device": device_serial}, env_extra=env_extra,
        )
        result.traces.append(device)
        result.device_status = device.status
    return result


__all__ = [
    "NETWORK_EXIT_CODE",
    "NETWORK_RETRY_DELAYS",
    "EvidencePreflight",
    "collect_deep_analysis_evidence",
]
