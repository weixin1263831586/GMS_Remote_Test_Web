from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from foundation.responses import error_response

from . import runtime
from .adb_forward import adb_forward_manager
from .models import ADBForwardStartRequest, USBIPDisconnectRequest, USBIPStartRequest
from .support import DeviceSSHConnection, format_device_list_info, notify_device_change
from .usbip import (
    USBIPD_INSTALL_CMD,
    USBIPD_INSTALL_GUIDE,
    detach_ubuntu_usbip_ports,
    find_device_host_password,
    usbip_manager,
)


logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/api/adb-forward/start")
async def start_adb_forward(req: ADBForwardStartRequest):
    """启动ADB转发"""
    try:
        result = adb_forward_manager.start_forward(req.device_host, req.device_password)
        if result.get('success'):
            return JSONResponse(content=result)
        return error_response(result.get('error', 'ADB转发启动失败'), status_code=500)
    except Exception as e:
        logger.error(f"Error starting ADB forward: {e}")
        return error_response(f"{e!s}. 请检查配置和参数是否正确。", status_code=500)


@router.post("/api/adb-forward/stop")
async def stop_adb_forward():
    """停止ADB转发"""
    try:
        result = adb_forward_manager.stop_forward('test_client')
        if result.get('success'):
            return JSONResponse(content=result)
        return error_response(result.get('error', 'ADB转发停止失败'), status_code=500)
    except Exception as e:
        logger.error(f"Error stopping ADB forward: {e}")
        return error_response(f"{e!s}. 请检查配置和参数是否正确。", status_code=500)


# ==================== USB/IP Status ====================

@router.get("/api/usbip/status")
async def get_usbip_status(
    request: Request,
    device_host: str | None = None,
):
    """Get USB/IP status (supports specifying host)."""
    if device_host:
        client_id = device_host
    else:
        client_id = runtime.get_client_id_from_request(request)
        tunnel_host, _ = runtime.resolve_tailscale_device_host(request, client_id)
        if tunnel_host:
            client_id = tunnel_host

    with runtime.global_state.usbip_states_lock:
        state_info = runtime.global_state.usbip_states.get(client_id, {"connected": False, "timestamp": 0})
        connected = state_info["connected"]

    if not connected:
        with runtime.global_state.usbip_devices_source_lock:
            has_devices_from_host = any(
                device_info.get("source") == client_id
                for device_info in runtime.global_state.usbip_devices_source.values()
            )
            if has_devices_from_host:
                connected = True

    logger.info(f"[USB/IP Status] client_id={client_id}, connected={connected}, device_count={len(runtime.global_state.usbip_devices_source)}")
    return JSONResponse(content={
        "connected": connected,
        "device_host": client_id,
        "device_count": len(runtime.global_state.usbip_devices_source),
    })


# ==================== USB/IP Connect ====================

@router.post("/api/usbip/connect")
async def start_usbip(
    request: Request,
    req: USBIPStartRequest | None = Body(default=None),
    help: bool = Query(False),
):
    resp = (
        runtime.generate_help_or_continue(help, "POST", "/api/usbip/connect")
        if runtime.generate_help_or_continue is not None
        else None
    )
    if resp:
        return resp

    try:
        config = runtime.config_manager.load_config()
        client_id = runtime.get_client_id_from_request(request)

        request_data = req.model_dump() if req else {}

        usbip_attach_host = None
        tunnel_host = None

        explicit_device_host = request_data.get("device_host")
        if explicit_device_host:
            device_host = explicit_device_host
        else:
            tunnel_host, tunnel_usbip_host = runtime.resolve_tailscale_device_host(request, client_id)
            if tunnel_host:
                device_host = tunnel_host
                usbip_attach_host = tunnel_usbip_host
                logger.info(f"[USB/IP] Tailscale direct mode: {device_host} attach={usbip_attach_host}")
            else:
                device_host = config.get("usbip_device_host") or config.get("device_host") or client_id

        logger.info(f"[USB/IP] Using device_host: {device_host}")
        try:
            from features.devices.reconnect import is_usbip_reconnect_suppressed
            if is_usbip_reconnect_suppressed(device_host) and not request_data.get("manual_connect"):
                return JSONResponse(content={
                    "success": False,
                    "manual_disconnect_suppressed": True,
                    "device_host": device_host,
                    "error": "USB/IP 已手动断开，自动重连已暂停；如需重新连接请点击本地设备。",
                })
        except Exception as e:
            logger.warning("[USB/IP] Failed to check reconnect suppression for %s: %s", device_host, e)

        windows_device_host = device_host

        submitted_device_password = request_data.get("device_password") or ""
        device_password = submitted_device_password or find_device_host_password(device_host, config) or config.get("device_pswd", "")
        if not device_password:
            return error_response(
                f"SSH credentials for {device_host} not found, please enter SSH password on login page",
                status_code=401,
                need_password=True,
                device_host=device_host,
            )

        result = await asyncio.to_thread(
            usbip_manager.start_usbip,
            device_host,
            device_password,
            usbip_attach_host=usbip_attach_host,
        )
        result["device_host"] = device_host

        if result.get("success"):
            device_list = result.get("device_list", [])
            if not device_list:
                result["success"] = False
                result["error"] = result.get("error") or "USB/IP attach 成功但尚未识别到 ADB 设备，继续等待设备恢复"
                return JSONResponse(content=result)

            if request_data.get("manual_connect"):
                try:
                    from features.devices.reconnect import clear_usbip_reconnect_suppression
                    clear_usbip_reconnect_suppression(device_host, device_list)
                except Exception as e:
                    logger.warning("[USB/IP] Failed to clear reconnect suppression for devices %s: %s", device_list, e)

            if submitted_device_password:
                try:
                    if runtime.config_manager.upsert_device_host_password(device_host, submitted_device_password):
                        logger.info(f"[USB/IP Start] Saved SSH credential for {device_host}")
                except Exception as e:
                    logger.warning(f"[USB/IP Start] Failed to save SSH credential for {device_host}: {e}")

            with runtime.global_state.usbip_states_lock:
                runtime.global_state.usbip_states[device_host] = {"connected": True, "timestamp": time.time()}
            logger.info(f"[USB/IP Start] Set connected=True for device_host={device_host}")

            if device_list:
                with runtime.global_state.usbip_devices_source_lock:
                    for device_id in device_list:
                        runtime.global_state.usbip_devices_source[device_id] = {
                            "source": windows_device_host,
                            "timestamp": time.time(),
                        }
                logger.info(f"[USB/IP Start] Recorded device source: {windows_device_host} for devices: {device_list}")

                # Persist USB/IP device sources to config
                try:
                    existing_runtime = runtime.config_manager.get_runtime_config()
                    usbip_sources = existing_runtime.get("usbip_devices_source", {})
                    for device_id in device_list:
                        usbip_sources[device_id] = {"source": windows_device_host, "timestamp": time.time()}
                    existing_runtime["usbip_devices_source"] = usbip_sources
                    if runtime.config_manager.save_runtime_config(existing_runtime):
                        logger.info(f"[USB/IP Start] Persisted device sources for {len(device_list)} devices")
                except Exception as e:
                    logger.warning(f"[USB/IP Start] Failed to persist device sources: {e}")

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting USB/IP: {e}")
        return error_response(str(e), status_code=500)


def _persist_device_source_removal(devices_to_remove: list):
    """Remove device IDs from runtime config's usbip_devices_source and save."""
    if not devices_to_remove:
        return
    try:
        existing_runtime = runtime.config_manager.get_runtime_config()
        usbip_sources = existing_runtime.get("usbip_devices_source", {})
        for device_id in devices_to_remove:
            if device_id in usbip_sources:
                del usbip_sources[device_id]
        existing_runtime["usbip_devices_source"] = usbip_sources
        if runtime.config_manager.save_runtime_config(existing_runtime):
            logger.info(f"[USB/IP Stop] Persisted device source removal for {len(devices_to_remove)} devices")
    except Exception as e:
        logger.warning(f"[USB/IP Stop] Failed to persist device source removal: {e}")


def _usbip_devices_for_host(device_host: str) -> list[str]:
    """Return known USB/IP device ids for a host from memory and runtime config."""
    devices = set()
    with runtime.global_state.usbip_devices_source_lock:
        for device_id, device_info in runtime.global_state.usbip_devices_source.items():
            if (device_info or {}).get("source") == device_host:
                devices.add(device_id)
    for device_id, source in (getattr(usbip_manager, "device_sources", {}) or {}).items():
        if (source or {}).get("source") == device_host:
            devices.add(device_id)
    try:
        runtime_sources = (runtime.config_manager.get_runtime_config() or {}).get("usbip_devices_source") or {}
        if isinstance(runtime_sources, dict):
            for device_id, device_info in runtime_sources.items():
                if (device_info or {}).get("source") == device_host:
                    devices.add(device_id)
    except Exception as e:
        logger.warning("[USB/IP Stop] Failed to read runtime USB/IP sources: %s", e)
    return list(devices)


def _clear_usbip_device_sources(
    device_host: str,
    devices_to_remove: list[str],
) -> None:
    devices_to_remove = list(dict.fromkeys(devices_to_remove or []))
    with runtime.global_state.usbip_devices_source_lock:
        for device_id in devices_to_remove:
            if device_id in runtime.global_state.usbip_devices_source:
                del runtime.global_state.usbip_devices_source[device_id]
                logger.info(f"[USB/IP Stop] Removed device source: {device_id} from {device_host}")

    for device_id in devices_to_remove:
        if device_id in usbip_manager.device_sources:
            del usbip_manager.device_sources[device_id]

    _persist_device_source_removal(devices_to_remove)


# ==================== USB/IP Disconnect ====================

@router.post("/api/usbip/disconnect")
async def stop_usbip(
    request: Request,
    req: USBIPDisconnectRequest | None = Body(default=None),
):
    """Stop USB/IP forwarding (supports specifying host)."""
    config = runtime.config_manager.load_config()
    client_id = runtime.get_client_id_from_request(request)
    tailscale_mode = False

    if req and req.device_host:
        config["device_host"] = req.device_host
    else:
        tunnel_host, tunnel_usbip_host = runtime.resolve_tailscale_device_host(request, client_id)
        if tunnel_host:
            config["device_host"] = tunnel_host
            config["usbip_attach_host"] = tunnel_usbip_host
            tailscale_mode = True
        else:
            config["device_host"] = client_id

    device_password = find_device_host_password(config["device_host"], config)
    if not device_password:
        device_password = config.get("device_pswd", "")
    if device_password:
        config["device_pswd"] = device_password

    devices_to_remove: list[str] = []
    usbip_attach_host = config.get("usbip_attach_host")

    try:
        from features.devices.reconnect import suppress_usbip_reconnect

        devices_to_remove = _usbip_devices_for_host(config["device_host"])
        suppress_usbip_reconnect(config["device_host"], devices_to_remove)

        if tailscale_mode:
            ubuntu_ssh = usbip_manager.runtime.ssh_manager.get_connection(config)
            if ubuntu_ssh:
                try:
                    detach_ubuntu_usbip_ports(ubuntu_ssh, usbip_attach_host or "127.0.0.1", detach_all=True)
                    usbip_manager.runtime.ssh_manager.return_connection(ubuntu_ssh)
                except Exception as e:
                    ubuntu_ssh.close()
                    logger.warning(f"[USB/IP Stop] detach Ubuntu usbip ports failed: {e}")
            logger.info("[USB/IP Stop] Public mode keeps Windows usbipd bindings; only Ubuntu attach is detached")
            await asyncio.sleep(1)
            _clear_usbip_device_sources(config["device_host"], devices_to_remove)
        else:
            with DeviceSSHConnection(config) as win_ssh:
                runtime.ssh_manager.execute_command(win_ssh, "usbipd unbind --all", timeout=10)
                await asyncio.sleep(2)

            _clear_usbip_device_sources(config["device_host"], devices_to_remove)

        with runtime.global_state.usbip_states_lock:
            runtime.global_state.usbip_states[config["device_host"]] = {"connected": False, "timestamp": time.time()}

        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")

        await notify_device_change(devices_to_remove, "USB/IP Stop")

        return JSONResponse(content={"success": True, "message": f"Local devices disconnected{disconnected_devices_info}"})

    except HTTPException:
        # Cannot connect to Windows, just clear connection state and device source records
        if not devices_to_remove:
            devices_to_remove = _usbip_devices_for_host(config["device_host"])
        try:
            from features.devices.reconnect import suppress_usbip_reconnect
            suppress_usbip_reconnect(config["device_host"], devices_to_remove)
        except Exception:
            pass
        _clear_usbip_device_sources(config["device_host"], devices_to_remove)

        with runtime.global_state.usbip_states_lock:
            runtime.global_state.usbip_states[config["device_host"]] = {"connected": False, "timestamp": time.time()}

        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")

        await notify_device_change(devices_to_remove, "USB/IP Stop")

        return JSONResponse(content={"success": True, "message": f"Local devices disconnected{disconnected_devices_info}"})


# ==================== USB/IP Install ====================

@router.post("/api/usbip/install")
async def install_usbipd(
    request: Request,
    device_host: str | None = None,
):
    """Install usbipd to Windows host."""
    try:
        config = runtime.config_manager.load_config()
        client_id = runtime.get_client_id_from_request(request)

        if device_host:
            config["device_host"] = device_host
        else:
            tunnel_host, _ = runtime.resolve_tailscale_device_host(request, client_id)
            if tunnel_host:
                windows_usbipd = await runtime.probe_windows_usbipd(tunnel_host)
                installed = bool(windows_usbipd.get("installed"))
                logger.info(f"[USB/IP Install] Tailscale SSH check: {tunnel_host}, installed={installed}")
                if installed:
                    return JSONResponse(content={
                        "success": True,
                        "installed": True,
                        "running": True,
                        "version": windows_usbipd.get("version") or "",
                        "message": f"usbipd installed{(', version: ' + windows_usbipd.get('version')) if windows_usbipd.get('version') else ''}",
                    })
                return JSONResponse(content={
                    "success": False,
                    "installed": False,
                    "running": False,
                    "install_guide": USBIPD_INSTALL_GUIDE.format(install_cmd=USBIPD_INSTALL_CMD),
                    "error": "Windows host does not have usbipd installed",
                })

            config["device_host"] = tunnel_host or client_id

        device_password = find_device_host_password(config["device_host"], config)
        if not device_password:
            device_password = config.get("device_pswd", "")
        if device_password:
            config["device_pswd"] = device_password

        with DeviceSSHConnection(config) as win_ssh:
            result = usbip_manager.install_usbipd(win_ssh, config)
            return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Error installing usbipd: {e}")
        return error_response(str(e), status_code=500)
