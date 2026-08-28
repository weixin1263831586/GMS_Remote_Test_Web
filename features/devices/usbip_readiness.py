"""Protocol readiness probes for attached USB/IP devices."""

from __future__ import annotations

import shlex
import time
from typing import Any


def wait_for_adb_serial_ready(
    ssh, serial_no: str, timeout: int = 30,
) -> dict[str, Any]:
    from .usbip import usbip_manager

    quoted_serial = shlex.quote(serial_no)
    deadline = time.time() + timeout
    last_output = last_error = ""
    execute = usbip_manager.ssh_manager.execute_command
    execute(ssh, "adb start-server", timeout=10)
    while time.time() < deadline:
        state_out, state_err, state_code = execute(
            ssh, f"adb -s {quoted_serial} get-state", timeout=8
        )
        state_text = (state_out or state_err or "").strip()
        last_output, last_error = state_out or "", state_err or ""
        if state_code == 0 and state_text == "device":
            shell_out, shell_err, shell_code = execute(
                ssh, f"adb -s {quoted_serial} shell echo ready", timeout=10
            )
            last_output, last_error = shell_out or "", shell_err or ""
            if shell_code == 0 and "ready" in shell_out:
                return {"ready": True}
        time.sleep(2)
    devices_out, devices_err, _ = execute(ssh, "adb devices", timeout=8)
    return {
        "ready": False,
        "state": (last_output or last_error).strip(),
        "devices": (devices_out or devices_err or "").strip(),
    }
