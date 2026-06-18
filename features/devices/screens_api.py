from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import runtime
from .models import DeviceActionRequest
from .support import ssh_connection_failed_response
from .utils import DeviceUtils


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/devices/scrcpy")
async def show_device_screens(req: DeviceActionRequest):
    """Display device screen (launch scrcpy mirroring)."""
    try:
        devices = req.devices

        config = runtime.config_manager.load_config()
        ubuntu_user = runtime.config_manager.get_ubuntu_user(config)
        ubuntu_host = runtime.config_manager.get_ubuntu_host(config)

        if not devices:
            with runtime.ssh_manager.optional_connection(config) as ssh:
                if ssh:
                    try:
                        stdout, _stderr, code = runtime.ssh_manager.execute_command(
                            ssh, "adb devices", timeout=5
                        )
                        if code == 0 and stdout:
                            lines = stdout.strip().split("\n")[1:]
                            devices = [
                                line.split()[0]
                                for line in lines
                                if line.strip() and "\tdevice" in line
                            ]
                    except Exception:
                        pass

        if not devices:
            return JSONResponse(
                content={"success": False, "error": "No devices selected"},
                status_code=400,
            )

        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()

            try:
                vnc_check_cmd = (
                    f"curl -s -o /dev/null -w '%{{http_code}}' http://{ubuntu_host}:6080 --connect-timeout 3"
                )
                vnc_output, _, _ = runtime.ssh_manager.execute_command(ssh, vnc_check_cmd, timeout=5)
                vnc_available = vnc_output.strip() == "200"

                scrcpy_path = config.get("scrcpy_path", "")
                if scrcpy_path:
                    scrcpy_path = scrcpy_path.replace("${ubuntu_user}", ubuntu_user)
                    scrcpy_check_cmd = (
                        f"test -f '{scrcpy_path}' && echo 'exists' || echo 'not_found'"
                    )
                    scrcpy_output, _, scrcpy_code = runtime.ssh_manager.execute_command(
                        ssh, scrcpy_check_cmd
                    )

                    if "not_found" in scrcpy_output:
                        return JSONResponse(
                            content={
                                "success": False,
                                "error": f"scrcpy not found: {scrcpy_path}",
                                "instructions": "Please check scrcpy_path in config",
                            },
                            status_code=404,
                        )
                else:
                    scrcpy_check_cmd = "which scrcpy"
                    scrcpy_output, _, scrcpy_code = runtime.ssh_manager.execute_command(
                        ssh, scrcpy_check_cmd
                    )

                    if scrcpy_code != 0:
                        return JSONResponse(
                            content={
                                "success": False,
                                "error": "scrcpy not installed",
                                "instructions": "sudo apt-get install -y scrcpy",
                            },
                            status_code=404,
                        )
                    scrcpy_path = "scrcpy"

                results = []
                vnc_sessions = []

                existing_devices = []
                for device_id in devices:
                    is_healthy, pid_or_error = DeviceUtils.check_scrcpy_healthy(
                        ssh, device_id
                    )

                    if is_healthy and pid_or_error:
                        existing_devices.append(device_id)
                        logger.info(
                            f"Detected already mirrored device: {device_id} (PID: {pid_or_error})"
                        )
                    else:
                        DeviceUtils.kill_process(
                            ssh, f"scrcpy.*-s {device_id}"
                        )

                new_devices = [d for d in devices if d not in existing_devices]

                if not new_devices:
                    return JSONResponse(
                        content={
                            "success": True,
                            "message": f"All {len(devices)} devices already being mirrored",
                            "results": [
                                {
                                    "device": d,
                                    "started": False,
                                    "already_running": True,
                                }
                                for d in devices
                            ],
                            "vnc_sessions": [
                                {"device": d, "message": "Already running"} for d in devices
                            ],
                            "note": "All devices already being mirrored",
                        }
                    )

                positions = DeviceUtils.calculate_window_positions(
                    existing_devices + new_devices, max_window_width=350
                )

                for idx, device_id in enumerate(sorted(existing_devices + new_devices)):
                    if device_id not in new_devices:
                        continue

                    x_offset = positions["start_x"] + idx * (
                        positions["window_width"] + positions["horizontal_gap"]
                    )
                    y_offset = positions["start_y"]
                    window_width = positions["window_width"]
                    window_height = positions["window_height"]
                    cmd = (
                        f"export DISPLAY=:0 && "
                        f"if [ -f /run/user/1000/gdm/Xauthority ]; then "
                        f"export XAUTHORITY=/run/user/1000/gdm/Xauthority; "
                        f"else "
                        f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority; "
                        f"fi && "
                        f"(nohup {scrcpy_path} -s {device_id} "
                        f"--max-size 800 "
                        f"--stay-awake "
                        f"--window-title '{device_id}' "
                        f"--window-x {x_offset} "
                        f"--window-y {y_offset} "
                        f"--window-width {window_width} "
                        f"--window-height {window_height} "
                        f"> /tmp/scrcpy_{device_id}.log 2>&1 &)"
                    )

                    runtime.ssh_manager.execute_command(ssh, cmd, timeout=10)

                    await asyncio.sleep(0.3)
                    check_cmd = (
                        f"pgrep -f 'scrcpy.*-s {device_id}' && echo 'RUNNING' || echo 'NOT_RUNNING'"
                    )
                    check_output, _, _ = runtime.ssh_manager.execute_command(
                        ssh, check_cmd, timeout=5
                    )
                    is_started = "RUNNING" in check_output

                    results.append(
                        {
                            "device": device_id,
                            "started": is_started,
                            "position": {
                                "x": x_offset,
                                "y": y_offset,
                                "width": window_width,
                                "height": window_height,
                            },
                        }
                    )

                    vnc_sessions.append(
                        {
                            "device": device_id,
                            "url": (
                                f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true"
                                if vnc_available
                                else None
                            ),
                            "message": "VNC view available" if vnc_available else "Local display only",
                        }
                    )


                newly_started = [r["device"] for r in results if r.get("started")]
                failed_devices = [r["device"] for r in results if not r.get("started")]

                message_parts = []
                if newly_started:
                    message_parts.append(
                        f"Started {len(newly_started)} screen mirrors: {', '.join(newly_started)}"
                    )
                if failed_devices:
                    message_parts.append(
                        f"{len(failed_devices)} devices failed to start: {', '.join(failed_devices)}"
                    )

                message = "\n".join(message_parts) if message_parts else "Screen mirror started"

                return JSONResponse(
                    content={
                        "success": len(failed_devices) == 0,
                        "message": message,
                        "results": results,
                        "vnc_sessions": vnc_sessions,
                        "desktop_url": "/desktop",
                        "note": (
                            'Click "Host Desktop" to view screens'
                            if vnc_available
                            else "VNC not started, screen only shown locally"
                        ),
                    }
                )
            except Exception:
                raise

    except Exception as e:
        logger.error(f"Error showing device screens: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)}, status_code=500
        )
