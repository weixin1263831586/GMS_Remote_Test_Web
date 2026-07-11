from __future__ import annotations

import re
import shlex


_SAFE_PGID_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def build_process_group_id(client_id: str, timestamp_ms: int) -> str:
    safe_client = _SAFE_PGID_CHARS.sub("_", client_id).strip("_") or "unknown"
    return f"gms_test_{safe_client}_{timestamp_ms}"


def parse_pid_lines(output: str) -> list[str]:
    """Return only valid decimal PIDs from ps/awk output."""
    pids: list[str] = []
    for line in (output or "").splitlines():
        pid = line.strip()
        if pid.isdecimal() and int(pid) > 1:
            pids.append(pid)
    return pids


def find_env_pgid_command(process_group_id: str) -> str:
    quoted = shlex.quote(f"GMS_TEST_PGID={process_group_id}")
    return f"ps eww -e | grep -F -- {quoted} | grep -v grep | awk '{{print $1}}'"


def find_arg_pgid_command(process_group_id: str) -> str:
    quoted = shlex.quote(f"--pgid {process_group_id}")
    return f"ps aux | grep -- {quoted} | grep -v grep | awk '{{print $2}}'"


def kill_pid_tree_commands(pid: str) -> list[str]:
    if not pid.isdecimal() or int(pid) <= 1:
        return []
    return [
        f"kill -9 {pid} 2>/dev/null",
        f"pkill -9 -P {pid} 2>/dev/null",
    ]
