from __future__ import annotations

import asyncio
import logging
import shlex

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from foundation.novnc import novnc_url
from foundation.security import sanitize_device_ids

from . import runtime
from .models import DeviceActionRequest
from .support import (
    device_claim_conflict_response,
    device_mutation_guard,
    ssh_connection_failed_response,
)
from .utils import DeviceUtils


logger = logging.getLogger(__name__)
router = APIRouter()

# 投屏就绪判定窗口：scrcpy 版本会打印 Connected 或 Device；轮询兜住慢设备。
_SCRCPY_START_POLL_ATTEMPTS = 8
_SCRCPY_START_POLL_INTERVAL = 0.5


async def _verify_scrcpy_started(ssh, device_id: str) -> tuple[bool, str]:
    """启动后确认投屏真正就绪，而不是只看进程存在。

    仅凭 pgrep 会在 scrcpy 随后立即失败（设备离线/ADB 未授权/编码失败）
    时误报成功；这里复用 check_scrcpy_healthy（进程状态 + 版本兼容的
    就绪日志）短轮询，失败时带回日志尾部，让用户能看到真实原因。
    """
    await asyncio.sleep(_SCRCPY_START_POLL_INTERVAL)
    for _ in range(_SCRCPY_START_POLL_ATTEMPTS):
        is_healthy, _pid_or_error = await asyncio.to_thread(
            DeviceUtils.check_scrcpy_healthy, ssh, device_id,
        )
        if is_healthy:
            return True, ""
        await asyncio.sleep(_SCRCPY_START_POLL_INTERVAL)
    # 同步函数：由 to_thread 调用（async 版本会把协程对象当返回值塞进响应）。
    return False, _scrcpy_log_tail(ssh, device_id)


def _scrcpy_log_tail(ssh, device_id: str) -> str:
    """抓取 scrcpy 日志尾部作为失败原因（压成单行、截断）。"""
    try:
        from foundation.ssh_executor import ssh_executor

        result = ssh_executor.run(
            ssh,
            f"tail -c 400 {shlex.quote(DeviceUtils.scrcpy_log_path(device_id))}",
            timeout=10,
        )
        tail = " ".join((result.stdout or "").split())
        return tail[:300]
    except Exception:
        return ""


@router.post("/api/devices/scrcpy")
@device_mutation_guard("scrcpy")
async def show_device_screens(req: DeviceActionRequest, request: Request):
    """Display device screen (launch scrcpy mirroring)."""
    try:
        devices = sanitize_device_ids(req.devices or [])

        config = runtime.config_manager.load_config()
        ubuntu_user = runtime.config_manager.get_ubuntu_user(config)

        if not devices:
            return JSONResponse(
                content={
                    "success": False,
                    "error": "An explicit device selection is required",
                },
                status_code=400,
            )
        conflict = device_claim_conflict_response(
            devices,
            runtime.get_client_id_from_request(request),
            allow_owner=True,
        )
        if conflict:
            return conflict

        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()

            try:
                # websockify 只绑定 127.0.0.1（浏览器经桌面页同源代理访问
                # noVNC），探测必须在测试主机本机对 loopback 发起；对
                # ubuntu_host 的外部地址探测会连接拒绝，把可用 VNC 误报为
                # "Local display only"。
                vnc_check_cmd = (
                    f"curl -s -o /dev/null -w '%{{http_code}}' "
                    f"{novnc_url('127.0.0.1', autoconnect=False)} --connect-timeout 3"
                )
                vnc_result = await asyncio.to_thread(
                    runtime.ssh_manager.execute_command, ssh, vnc_check_cmd, timeout=5,
                )
                vnc_available = vnc_result.stdout.strip() == "200"

                scrcpy_path = config.get("scrcpy_path", "")
                if scrcpy_path:
                    scrcpy_path = scrcpy_path.replace("${ubuntu_user}", ubuntu_user)
                    scrcpy_check_cmd = f"test -f {shlex.quote(scrcpy_path)} && echo 'exists' || echo 'not_found'"
                    scrcpy_result = await asyncio.to_thread(
                        runtime.ssh_manager.execute_command, ssh, scrcpy_check_cmd,
                    )

                    if "not_found" in scrcpy_result.stdout:
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
                    scrcpy_result = await asyncio.to_thread(
                        runtime.ssh_manager.execute_command, ssh, scrcpy_check_cmd,
                    )

                    if not scrcpy_result.ok:
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
                        DeviceUtils.kill_process(ssh, DeviceUtils.scrcpy_process_pattern(device_id))

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
                    cmd = DeviceUtils.build_scrcpy_command(
                        scrcpy_path=scrcpy_path,
                        device_id=device_id,
                        ubuntu_user=ubuntu_user,
                        x_offset=x_offset,
                        y_offset=y_offset,
                        window_width=window_width,
                        window_height=window_height,
                        use_gdm_xauthority_fallback=True,
                        background=True,
                    )

                    await asyncio.to_thread(
                        runtime.ssh_manager.execute_command, ssh, cmd, timeout=10,
                    )

                    is_started, health_error = await _verify_scrcpy_started(
                        ssh, device_id,
                    )

                    result_entry: dict = {
                        "device": device_id,
                        "started": is_started,
                        "position": {
                            "x": x_offset,
                            "y": y_offset,
                            "width": window_width,
                            "height": window_height,
                        },
                    }
                    if not is_started and health_error:
                        result_entry["error"] = health_error
                    results.append(result_entry)

                    vnc_sessions.append(
                        {
                            "device": device_id,
                            # 不返回 http://<host>:6080 直连 URL：websockify
                            # 仅监听 loopback，浏览器应经桌面页的同源代理
                            # （/novnc/vnc.html + access_token）访问。
                            "message": "VNC view available" if vnc_available else "Local display only",
                        }
                    )


                failed_devices = [r["device"] for r in results if not r.get("started")]

                # 逐设备结果行（前端按 ✅/❌ 前缀着色），失败行附带真实原因。
                message_parts = []
                for entry in results:
                    device = entry["device"]
                    if entry.get("started"):
                        message_parts.append(f"✅ 设备 {device}: 投屏已就绪")
                    else:
                        detail = str(entry.get("error") or "进程未就绪（无 scrcpy 就绪日志）").strip()
                        message_parts.append(f"❌ 设备 {device}: 投屏启动失败 - {detail}")

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
