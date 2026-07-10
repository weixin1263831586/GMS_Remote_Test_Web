from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading


logger = logging.getLogger(__name__)


def command_reports_running(output: str | None) -> bool:
    """Return True if a status command's output contains an exact ``RUNNING`` line."""
    return any(line.strip() == 'RUNNING' for line in (output or '').splitlines())


def run_local_shell_command(command: str, timeout: int = 30) -> tuple[str, str, int]:
    """Run a local shell command and return stdout, stderr, and exit code.

    Keep shell-string execution behind this helper so call sites can be audited
    and migrated to argv-based subprocess calls incrementally.
    """
    process = None
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout, stderr, process.returncode
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
        return "", "Command timed out", -1
    except Exception as exc:
        return "", str(exc), -1


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
