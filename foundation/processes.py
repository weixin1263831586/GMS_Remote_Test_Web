from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading

from foundation.command_result import CommandResult
from foundation.redaction import redact_sensitive_text


logger = logging.getLogger(__name__)


def command_reports_running(output: str | None) -> bool:
    """Return True if a status command's output contains an exact ``RUNNING`` line."""
    return any(line.strip() == 'RUNNING' for line in (output or '').splitlines())


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill a Popen started with start_new_session=True, whole group included."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception as exc:
        logger.warning("Failed to kill command process group: %s", exc)
    with contextlib.suppress(Exception):
        process.communicate(timeout=1)


class _CommandCancelledError(Exception):
    """Internal: raised when a caller-provided cancel event fires."""


def _communicate_cancellable(
    process: subprocess.Popen, timeout: int, cancel_event: threading.Event,
) -> tuple[str, str]:
    """communicate() variant that aborts within ~1s of the cancel event firing.

    Polls in short intervals so a shutdown can stop a long scan subprocess
    (radar scans run up to timeout=3600s) instead of waiting it out. Raises
    subprocess.TimeoutExpired once the overall timeout elapses, mirroring the
    plain communicate(timeout=...) contract.
    """
    poll = 1.0
    waited = 0.0
    while True:
        try:
            return process.communicate(timeout=poll)
        except subprocess.TimeoutExpired:
            waited += poll
            if cancel_event.is_set():
                raise _CommandCancelledError from None
            if waited >= timeout:
                raise


def run_local_command(
    argv: list[str], timeout: int = 30, *,
    env: dict[str, str] | None = None, cwd: str | None = None,
    cancel_event: threading.Event | None = None,
) -> CommandResult:
    """Run a local command from an argv list (no shell interpretation).

    Preferred over :func:`run_local_shell_command`: argv form has no shell
    metacharacter surface, so callers cannot accidentally introduce injection
    via unquoted dynamic values. 与 SSH 侧 ``SSHExecutor`` 一致，统一返回
    :class:`CommandResult`，杜绝 ``(stdout, stderr, code)`` 裸 tuple 的
    位置错用。

    ``cancel_event``: optional shutdown signal; when set while the command is
    running, the process group is killed and the result reports a non-zero
    exit with ``stderr='Command cancelled'`` so callers can persist an
    interrupted state instead of orphaning the subprocess.
    """
    process = None
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
            cwd=cwd,
        )
        if cancel_event is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            try:
                stdout, stderr = _communicate_cancellable(process, timeout, cancel_event)
            except _CommandCancelledError:
                _kill_process_group(process)
                return CommandResult(stdout="", stderr="Command cancelled", code=-1)
        return CommandResult(
            stdout=stdout, stderr=stderr, code=process.returncode,
        )
    except subprocess.TimeoutExpired:
        if process is not None:
            _kill_process_group(process)
        return CommandResult(stdout="", stderr="Command timed out", code=-1)
    except Exception as exc:
        return CommandResult(stdout="", stderr=redact_sensitive_text(exc), code=-1)


def run_local_shell_command(command: str, timeout: int = 30) -> CommandResult:
    """Run a local shell command and return a :class:`CommandResult`.

    Keep shell-string execution behind this helper so call sites can be audited
    and migrated to argv-based subprocess calls incrementally. 结果类型与
    SSH 侧 ``SSHExecutor`` 一致（``result.stdout``/``result.ok``），本节
    重构后本地与远程执行不再有第二套结果形态。
    """
    process = None
    try:
        # 审计过的 shell-string 边界（见上方 docstring）：调用方负责转义
        # 动态 token，本地 shell 出口收敛在此与 features/build/executor.py
        # 两处；远程执行统一经 SSHExecutor。
        process = subprocess.Popen(  # nosemgrep: gitlab.bandit.B602
            command,
            # Callers use this boundary for audited pipelines/redirection and
            # quote every dynamic token before it reaches the local shell.
            shell=True,  # nosec B602  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        return CommandResult(
            stdout=stdout, stderr=stderr, code=process.returncode,
        )
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.warning("Failed to kill timed out command process group: %s", exc)
            with contextlib.suppress(Exception):
                process.communicate(timeout=1)
        return CommandResult(stdout="", stderr="Command timed out", code=-1)
    except Exception as exc:
        return CommandResult(stdout="", stderr=redact_sensitive_text(exc), code=-1)


def start_detached_process(
    args: list[str],
    *,
    stdout=None,
    stderr=None,
    name: str | None = None,
) -> subprocess.Popen:
    """Start a long-lived local process and reap it when it exits."""
    process = subprocess.Popen(
        args,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )

    def _reap() -> None:
        return_code = process.wait()
        logger.debug("Detached process %s exited with code %s", name or args[0], return_code)

    thread = threading.Thread(
        target=_reap,
        name=f"reap_{name or args[0]}_{process.pid}",
        daemon=True,
    )
    thread.start()
    return process
