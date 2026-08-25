"""Elevated USB/IP installation endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from features.auth import require_permission_when_auth_required
from features.users import get_client_display_id_from_request
from foundation.responses import error_response

from . import runtime
from .support import DeviceSSHConnection
from .usbip import (
    USBIPD_INSTALL_CMD,
    USBIPD_INSTALL_GUIDE,
    find_device_host_password,
    usbip_manager,
)


logger = logging.getLogger(__name__)
router = APIRouter()


def resolve_install_host(request: Request, config: dict, client_id: str) -> str:
    tunnel_host, _ = runtime.resolve_tailscale_device_host(request, client_id)
    if tunnel_host:
        return tunnel_host
    return str(
        config.get("usbip_device_host")
        or config.get("device_host")
        or get_client_display_id_from_request(request)
        or ""
    ).strip()


@router.post("/api/usbip/install")
async def install_usbipd(
    request: Request,
    device_host: str | None = None,
    _operator=Depends(require_permission_when_auth_required("devices.lease")),
):
    """Install usbipd on an authorized Windows host."""

    try:
        config = runtime.config_manager.load_config()
        client_id = runtime.get_client_id_from_request(request)
        selected_host = str(device_host or "").strip()
        if not selected_host:
            tunnel_host, _ = runtime.resolve_tailscale_device_host(
                request,
                client_id,
            )
            if tunnel_host:
                windows_usbipd = await runtime.probe_windows_usbipd(tunnel_host)
                installed = bool(windows_usbipd.get("installed"))
                logger.info(
                    "[USB/IP Install] Tailscale SSH check: %s, installed=%s",
                    tunnel_host,
                    installed,
                )
                if installed:
                    version = str(windows_usbipd.get("version") or "")
                    return JSONResponse(content={
                        "success": True,
                        "installed": True,
                        "running": True,
                        "version": version,
                        "message": f"usbipd installed{', version: ' + version if version else ''}",
                    })
                return JSONResponse(content={
                    "success": False,
                    "installed": False,
                    "running": False,
                    "install_guide": USBIPD_INSTALL_GUIDE.format(
                        install_cmd=USBIPD_INSTALL_CMD
                    ),
                    "error": "Windows host does not have usbipd installed",
                })
            selected_host = resolve_install_host(request, config, client_id)

        if not selected_host or "@" not in selected_host:
            logger.error(
                "[USB/IP Install] No reachable Windows host for client %s",
                client_id,
            )
            return error_response(
                "无法识别 Windows 设备主机，请先在主机目录中配置授权主机",
                status_code=400,
            )
        config["device_host"] = selected_host
        password = (
            find_device_host_password(selected_host, config)
            or config.get("device_pswd", "")
        )
        if password:
            config["device_pswd"] = password

        with DeviceSSHConnection(config) as win_ssh:
            return JSONResponse(
                content=usbip_manager.install_usbipd(win_ssh, config)
            )
    except Exception as exc:
        logger.error("Error installing usbipd: %s", exc)
        return error_response(str(exc), status_code=500)


__all__ = ["install_usbipd", "router"]
