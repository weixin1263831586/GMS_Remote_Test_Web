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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .auth_preflight import GMS_SELFCHECK_SCRIPT
from .process import child_env, terminate_process_tree

# _attachment_manifest 只提取清单元数据（artifact id/kind/status），不含
# 客户正文——与 trace._record_tool_result 对 live trace 的记账口径一致。
from .trace import ToolTrace, _attachment_manifest


NETWORK_EXIT_CODE = 6
CANCELLED_EXIT_CODE = 130
PREFLIGHT_TIMEOUT_SECONDS = 90.0
CANCEL_POLL_INTERVAL_SECONDS = 0.25
# 网络类失败最多尝试 3 次，尝试之间按 (1.0s, 3.0s) 退避；单个命令的
# 最坏 wall time ≈ 3 × PREFLIGHT_TIMEOUT_SECONDS + 4s。非网络失败
# （含 usage error / 业务失败）不重试，立即收敛。
NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_BACKOFF_SECONDS = (1.0, 3.0)


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


async def _terminate_and_settle(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None,
) -> None:
    """Terminate one preflight process and settle its communicate task."""
    await terminate_process_tree(process)
    if communicate_task is None or communicate_task.done():
        return
    communicate_task.cancel()
    with suppress(asyncio.CancelledError):
        await communicate_task


async def _run_readonly_command(
    command: list[str], env_extra: dict[str, str], *, timeout_seconds: float,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[int, bytes, str]:
    """Run one bounded read-only CLI call, interruptible by persisted cancel.

    Without ``should_cancel`` this keeps the normal one-shot ``wait_for`` path.
    With a callback, the communicate future is polled at a short bounded
    interval so a UI/API cancel request terminates the *currently running* CLI
    process instead of waiting up to the full 90-second command timeout.
    """
    process: asyncio.subprocess.Process | None = None
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(env_extra),
            start_new_session=os.name == "posix",
        )
        if should_cancel is None:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds,
            )
        else:
            communicate_task = asyncio.create_task(process.communicate())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_seconds
            while True:
                if should_cancel():
                    await _terminate_and_settle(process, communicate_task)
                    return CANCELLED_EXIT_CODE, b"", "Controller evidence request cancelled"
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await _terminate_and_settle(process, communicate_task)
                    return NETWORK_EXIT_CODE, b"", "Controller evidence request timed out"
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate_task),
                        timeout=min(CANCEL_POLL_INTERVAL_SECONDS, remaining),
                    )
                    break
                except asyncio.TimeoutError:
                    continue
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
            cleanup = asyncio.create_task(
                _terminate_and_settle(process, communicate_task)
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        raise


async def _sleep_with_cancel(
    delay: float,
    should_cancel: Callable[[], bool] | None,
) -> bool:
    """Sleep a retry backoff while allowing persisted cancellation to cut it short."""
    if delay <= 0:
        return bool(should_cancel and should_cancel())
    if should_cancel is None:
        await asyncio.sleep(delay)
        return False
    remaining = delay
    while remaining > 0:
        if should_cancel():
            return True
        step = min(CANCEL_POLL_INTERVAL_SECONDS, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return bool(should_cancel())


async def _collect(
    *, tool_name: str, arguments: list[str], tool_input: dict[str, Any],
    env_extra: dict[str, str], evidence_issue_ids: list[int] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[ToolTrace, dict[str, Any]]:
    """Run one preflight command; retry only documented network failures.

    ``should_cancel`` is checked before attempts, during retry backoff and while
    the current CLI process is running. A cancellation terminates that process
    and never starts a later retry.
    """
    command = _gms_command(tool_name.replace("gms_rt_", "gms-rt-").replace("_", "-"), arguments)
    if command is None:
        return _summary_trace(
            tool_name=tool_name, tool_input=tool_input, status="failed",
            error="GMS evidence CLI is unavailable", evidence_issue_ids=evidence_issue_ids,
        ), {}
    output = b""
    error = ""
    exit_code = NETWORK_EXIT_CODE
    for attempt in range(NETWORK_RETRY_ATTEMPTS):
        if should_cancel is not None and should_cancel():
            exit_code = CANCELLED_EXIT_CODE
            error = "cancelled before evidence preflight attempt"
            break
        if attempt and await _sleep_with_cancel(
            NETWORK_RETRY_BACKOFF_SECONDS[attempt - 1], should_cancel
        ):
            exit_code = CANCELLED_EXIT_CODE
            error = "cancelled during evidence preflight retry backoff"
            break
        exit_code, output, error = await _run_readonly_command(
            command,
            env_extra,
            timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
            should_cancel=should_cancel,
        )
        if exit_code == CANCELLED_EXIT_CODE:
            break
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
            "" if success else "cancelled"
            if exit_code == CANCELLED_EXIT_CODE else "service_unavailable"
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
    should_cancel: Callable[[], bool] | None = None,
) -> EvidencePreflight:
    """Collect the mandatory immutable evidence baseline for a single issue.

    Redmine fetch/journals/attachments and the optional device snapshot are all
    idempotent, read-only evidence operations. A failed preflight is recorded
    but does not suppress the later analysis: the final report can accurately
    state which source was unavailable. Cancellation is checked while an
    evidence CLI is in flight as well as at command/retry boundaries.
    """
    result = EvidencePreflight()
    fetched, data = await _collect(
        tool_name="gms_rt_redmine_issue_fetch",
        arguments=[str(issue_id), "--wait", "--json", "--non-interactive"],
        tool_input={"issue_id": issue_id}, env_extra=env_extra,
        evidence_issue_ids=[issue_id],
        should_cancel=should_cancel,
    )
    result.traces.append(fetched)
    result.snapshot_id = str(data.get("snapshot_id") or "")
    if result.snapshot_id:
        journals, _ = await _collect(
            tool_name="gms_rt_redmine_journals",
            arguments=[result.snapshot_id, "--json", "--non-interactive"],
            tool_input={"snapshot_id": result.snapshot_id}, env_extra=env_extra,
            evidence_issue_ids=[issue_id],
            should_cancel=should_cancel,
        )
        attachments, data = await _collect(
            tool_name="gms_rt_redmine_attachments",
            arguments=[result.snapshot_id, "--json", "--non-interactive"],
            tool_input={"snapshot_id": result.snapshot_id}, env_extra=env_extra,
            evidence_issue_ids=[issue_id],
            should_cancel=should_cancel,
        )
        if attachments.succeeded and isinstance(data, dict):
            # 把清单元数据记入 trace：否则 Evidence Gate 会对这条"成功"
            # 的预采集调用报 "manifest result was not structured JSON"，
            # 而修复提示又禁止模型重复已成功的取证——两个信号互相矛盾，
            # 附件类 issue 会系统性烧光修复轮。记账后 gate 能把"列过未
            # 读"精确转成模型该用 MCP 读取哪些 artifact。
            (
                attachments.attachment_manifest_parsed,
                attachments.attachment_count,
                attachments.text_artifact_ids,
                attachments.all_artifact_ids,
            ) = _attachment_manifest(data)
        result.traces.extend([journals, attachments])
    if device_serial:
        device, _ = await _collect(
            tool_name="gms_rt_devices_snapshot",
            arguments=[device_serial, "--json", "--non-interactive"],
            tool_input={"device": device_serial}, env_extra=env_extra,
            should_cancel=should_cancel,
        )
        result.traces.append(device)
        result.device_status = device.status
    return result


__all__ = [
    "CANCELLED_EXIT_CODE",
    "NETWORK_EXIT_CODE",
    "NETWORK_RETRY_ATTEMPTS",
    "EvidencePreflight",
    "collect_deep_analysis_evidence",
]
