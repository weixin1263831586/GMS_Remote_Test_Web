"""Read only whitelisted disk-root environment from the on-host Controller.

Never serialize the service's complete environment: it may contain secrets.
Fall back to Runner process settings when the service process is inaccessible.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


_ROOT_KEYS = frozenset(("GMS_DATA_ROOT", "GMS_UPLOAD_DIR", "GMS_TEST_LOG_ROOT"))


def active_disk_root_environment() -> tuple[dict[str, str], str]:
    runner = {key: value for key in _ROOT_KEYS
              if (value := os.environ.get(key))}
    try:
        process = subprocess.run(
            ("systemctl", "show", "gms-web-app.service",
             "--property=MainPID", "--value"),
            capture_output=True, text=True, check=False, timeout=3,
        )
        pid_text = process.stdout.strip()
        if process.returncode != 0 or not pid_text.isdecimal() or int(pid_text) <= 1:
            return runner, "Runner environment (Controller PID unavailable)"
        # The raw bytes are never logged or returned. Extract only path keys.
        raw = (Path("/proc") / pid_text / "environ").read_bytes()
        service: dict[str, str] = {}
        for field in raw.split(b"\0"):
            name, separator, value = field.partition(b"=")
            if separator:
                key = name.decode("ascii", errors="ignore")
                if key in _ROOT_KEYS and value:
                    service[key] = os.fsdecode(value)
        return service, "gms-web-app.service process environment"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return runner, "Runner environment (service environment unreadable)"
