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
from .process import child_env, settle_reader_future, terminate_process_tree

# _attachment_manifest 只提取清单元数据（artifact id/kind/status），不含
# 客户正文——与 trace._record_tool_result 对 live trace 的记账口径一致。
from .trace import ToolTrace, _attachment_manifest


NETWORK_EXIT_CODE = 6
PREFLIGHT_TIMEOUT_SECONDS = 90.0
# 网络类失败最多尝试 3 次，尝试之间按 (1.0s, 3.0s) 退避；单个命令的
# 最坏 wall time ≈ 3 × PREFLIGHT_TIMEOUT_SECONDS + 4s。非网络失败
# （含 usage error / 业务失败）不重试，立即收敛。
#
# 与 Prompt 规范的重试分层：skill/references/
# redmine-daily-triage.md 的「瞬时失败只允许重试一次」约束的是**模型层**
# 工具调用；这里是 **Controller baseline 预采集**的确定性重试，
# 发生在模型启动之前，两者不是同一层的预算，因此不冲突：
#   * 模型层：同一工具瞬时失败由 LLM 自行决定，最多重试 1 次；
#   * Controller baseline：preflight 对网络退出码固定 3 attempts。
# 若未来统一为同一预算，必须同时改这里与 skill 文档，不能只改一侧。
NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_BACKOFF_SECONDS = (1.0, 3.0)
# 运行中取消轮询间隔：与 kkagent 正式阶段的 CANCEL_POLL_SECONDS 一致，
# 保证"点击停止 → 进程终止"延迟全阶段一致（评审 P2）。
CANCEL_POLL_SECONDS = 0.25


class PreflightCancelledError(Exception):
    """用户在 preflight CLI 运行中请求停止（区别于超时/网络失败）。"""


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


async def _wait_cancel_flag(should_cancel: Callable[[], bool]) -> bool:
    """轮询持久化取消标志；命中后以完成态唤醒 asyncio.wait。"""
    while True:
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        if should_cancel():
            return True


async def _run_readonly_command(
    command: list[str], env_extra: dict[str, str], *, timeout_seconds: float,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[int, bytes, str]:
    """Run one bounded, read-only CLI call and return its exit/output summary.

    ``should_cancel`` 与超时一起参与同一个 wait（评审 P2：此前用户在
    communicate 期间点击停止，最多要等满一次 90s 调用才能停止）。取消
    先到 → 立即整树终止 CLI 并抛 ``PreflightCancelledError``。
    """
    process: asyncio.subprocess.Process | None = None
    communicate: asyncio.Future | None = None
    cancel_watch: asyncio.Future | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(env_extra),
            start_new_session=os.name == "posix",
        )
        communicate = asyncio.ensure_future(process.communicate())
        if should_cancel is not None:
            cancel_watch = asyncio.ensure_future(_wait_cancel_flag(should_cancel))
        done, _pending = await asyncio.wait(
            {communicate, *([cancel_watch] if cancel_watch else [])},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_watch is not None and cancel_watch in done:
            # 取消优先于同时完成的 communicate：停止语义必须生效。
            communicate.cancel()
            await terminate_process_tree(process)
            await settle_reader_future(communicate)
            cancel_watch.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_watch
            raise PreflightCancelledError("cancelled during evidence preflight command")
        if cancel_watch is not None:
            cancel_watch.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_watch
        if communicate in done:
            stdout, stderr = communicate.result()
            return int(process.returncode or 0), stdout, stderr.decode(
                "utf-8", errors="replace"
            )[:500]
        # 超时分支：communicate 仍在 pending。
        await terminate_process_tree(process)
        await settle_reader_future(communicate)
        return NETWORK_EXIT_CODE, b"", "Controller evidence request timed out"
    except OSError as exc:
        return NETWORK_EXIT_CODE, b"", f"Controller evidence request unavailable: {exc}"
    except asyncio.CancelledError:
        if cancel_watch is not None:
            cancel_watch.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_watch
        if communicate is not None:
            communicate.cancel()
        if process is not None:
            cleanup = asyncio.create_task(terminate_process_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        if communicate is not None:
            await settle_reader_future(communicate)
        raise


async def _collect(
    *, tool_name: str, arguments: list[str], tool_input: dict[str, Any],
    env_extra: dict[str, str], evidence_issue_ids: list[int] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_tool_event: Callable[[str, dict[str, Any], str, bool], None] | None = None,
) -> tuple[ToolTrace, dict[str, Any]]:
    """Run one preflight command; retry only documented network failures.

    ``should_cancel`` 在每次尝试与退避间隙被轮询：返回 True 时立即返回
    ``failed/cancelled`` 轨迹（等价 run 级停止），不再启动新的 CLI 进程。
    ``on_tool_event(tool_name, tool_input, phase, ok)`` 在调用开始
    （``started``）与结束（``finished``）时回调，供实时进度时间线使用；
    回调异常不影响 preflight 本身。
    """
    command = _gms_command(tool_name.replace("gms_rt_", "gms-rt-").replace("_", "-"), arguments)
    if command is None:
        return _summary_trace(
            tool_name=tool_name, tool_input=tool_input, status="failed",
            error="GMS evidence CLI is unavailable", evidence_issue_ids=evidence_issue_ids,
        ), {}

    def _notify(phase: str, ok: bool = False) -> None:
        if on_tool_event is None:
            return
        try:
            on_tool_event(tool_name, tool_input, phase, ok)
        except Exception:
            pass

    _notify("started")
    output = b""
    error = ""
    exit_code = NETWORK_EXIT_CODE
    for attempt in range(NETWORK_RETRY_ATTEMPTS):
        if should_cancel is not None and should_cancel():
            trace, payload = _summary_trace(
                tool_name=tool_name, tool_input=tool_input, status="failed",
                error="cancelled before evidence preflight attempt",
                evidence_issue_ids=evidence_issue_ids,
            ), {}
            _notify("finished", False)
            return trace, payload
        if attempt:
            await asyncio.sleep(NETWORK_RETRY_BACKOFF_SECONDS[attempt - 1])
        try:
            exit_code, output, error = await _run_readonly_command(
                command, env_extra, timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
                should_cancel=should_cancel,
            )
        except PreflightCancelledError:
            # 用户在 CLI 运行中请求停止：与"尝试前取消"同一收敛路径，
            # 不再重试，也不启动后续 preflight 命令。
            trace, payload = _summary_trace(
                tool_name=tool_name, tool_input=tool_input, status="failed",
                error="cancelled during evidence preflight command",
                evidence_issue_ids=evidence_issue_ids,
            ), {}
            _notify("finished", False)
            return trace, payload
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
    _notify("finished", success)
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
    include_device: bool = True,
    should_cancel: Callable[[], bool] | None = None,
    on_tool_event: Callable[[str, dict[str, Any], str, bool], None] | None = None,
) -> EvidencePreflight:
    """Collect the mandatory immutable evidence baseline for a single issue.

    Redmine fetch/journals/attachments and the optional device snapshot are all
    idempotent, read-only evidence operations.  A failed preflight is recorded
    but does not suppress the later analysis: the final report can accurately
    state which source was unavailable.
    ``should_cancel``（run 级取消标志轮询）在每次 CLI 尝试与退避间隙被
    检查；用户请求停止时立即返回已收集的部分，而不是继续排队后续命令。
    ``on_tool_event`` 透传给每次 ``_collect``（实时进度时间线）。
    ``include_device=False``（晨报批量 triage）只收 Redmine 三件套基线：
    triage 证据门禁不含设备取证，模型也没有设备写入契约。
    """
    result = EvidencePreflight()
    fetched, data = await _collect(
        tool_name="gms_rt_redmine_issue_fetch",
        arguments=[str(issue_id), "--wait", "--json", "--non-interactive"],
        tool_input={"issue_id": issue_id}, env_extra=env_extra,
        evidence_issue_ids=[issue_id],
        should_cancel=should_cancel,
        on_tool_event=on_tool_event,
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
            on_tool_event=on_tool_event,
        )
        attachments, data = await _collect(
            tool_name="gms_rt_redmine_attachments",
            arguments=[result.snapshot_id, "--json", "--non-interactive"],
            tool_input={"snapshot_id": result.snapshot_id}, env_extra=env_extra,
            evidence_issue_ids=[issue_id],
            should_cancel=should_cancel,
            on_tool_event=on_tool_event,
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
    devices = (
        [item.strip() for item in str(device_serial or "").split(",") if item.strip()]
        if include_device else []
    )
    if devices:
        statuses: list[str] = []
        for serial in devices:
            device, _ = await _collect(
                tool_name="gms_rt_devices_snapshot",
                arguments=[serial, "--json", "--non-interactive"],
                tool_input={"device": serial}, env_extra=env_extra,
                should_cancel=should_cancel,
                on_tool_event=on_tool_event,
            )
            result.traces.append(device)
            statuses.append(str(device.status))
        result.device_status = (
            statuses[0] if len(statuses) == 1
            else ", ".join(f"{serial}:{status}" for serial, status in zip(devices, statuses))
        )
    return result


__all__ = [
    "NETWORK_EXIT_CODE",
    "NETWORK_RETRY_ATTEMPTS",
    "EvidencePreflight",
    "PreflightCancelledError",
    "collect_deep_analysis_evidence",
]
