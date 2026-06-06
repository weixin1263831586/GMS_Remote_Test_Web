"""Desktop/VNC routes - VNC status, start, stop, noVNC proxy, host validation."""

import asyncio
import logging
import os
from typing import Optional

import aiohttp
import paramiko
from fastapi import APIRouter, Body, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from core.api_response import error_response, success_response
from core.common_utils import CommonUtils
from core.config import config_manager
from core.error_handling import handle_api_errors
from core.schemas import VNCStartRequest
from core.ssh import ssh_manager
from core.vnc import calculate_window_positions, vnc_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# noVNC upstream URLs
NOVNC_UPSTREAM_HTTP = os.getenv("GMS_NOVNC_UPSTREAM", "http://127.0.0.1:6080").rstrip("/")
NOVNC_UPSTREAM_WS = NOVNC_UPSTREAM_HTTP.replace("http://", "ws://", 1).replace("https://", "wss://", 1)


def build_novnc_upstream_url(path: str, query_string: bytes = b"") -> str:
    upstream_path = path.lstrip("/") or "vnc.html"
    url = f"{NOVNC_UPSTREAM_HTTP}/{upstream_path}"
    if query_string:
        url = f"{url}?{query_string.decode('utf-8', errors='ignore')}"
    return url


# ==================== VNC Routes ====================

@router.get("/api/desktop/vnc/status")
async def get_desktop_vnc_status():
    """获取VNC状态"""
    try:
        result = vnc_manager.get_vnc_status()
        return success_response(result)
    except Exception as e:
        logger.error(f"Error getting VNC status: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )


@router.post("/api/desktop/vnc/start")
async def start_desktop_vnc(req: Optional[VNCStartRequest] = Body(default=None)):
    """启动Ubuntu主机桌面VNC（Ubuntu桌面的VNC服务）"""
    config = config_manager.load_config()
    default_host = f"{config_manager.get_ubuntu_user(config)}@{config_manager.get_ubuntu_host(config) or 'localhost'}"

    if req is None:
        host = default_host
        password = config.get('ubuntu_pswd', '')
        vnc_password = ''
        force_restart = False
    else:
        host = req.host or default_host
        password = req.password or (config.get('ubuntu_pswd', '') if host == default_host else '')
        vnc_password = req.vnc_password or ''
        force_restart = req.force_restart

    result = vnc_manager.start_vnc(host, password, vnc_password, force_restart=force_restart)
    return JSONResponse(content=result)


@router.post("/api/vnc/start")
async def start_desktop_vnc_legacy(req: Optional[VNCStartRequest] = Body(default=None)):
    """/api/vnc/start。"""
    return await start_desktop_vnc(req)


@router.post("/api/desktop/vnc/stop")
async def stop_desktop_vnc():
    """停止Ubuntu主机桌面VNC"""
    result = vnc_manager.stop_vnc()
    return JSONResponse(content=result)


# ==================== noVNC WebSocket Proxy ====================

@router.websocket("/websockify")
@router.websocket("/novnc/websockify")
@router.websocket("/novnc/novnc/websockify")
async def novnc_websockify_proxy(websocket: WebSocket):
    """Proxy noVNC websocket through the 5001 origin for Tailscale/remote access."""
    await websocket.accept()
    upstream_url = f"{NOVNC_UPSTREAM_WS}/websockify"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(upstream_url) as upstream:
                async def client_to_upstream():
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            await upstream.close()
                            break
                        if message.get("bytes") is not None:
                            await upstream.send_bytes(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send_str(message["text"])

                async def upstream_to_client():
                    async for message in upstream:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            await websocket.send_bytes(message.data)
                        elif message.type == aiohttp.WSMsgType.TEXT:
                            await websocket.send_text(message.data)
                        elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client())
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
    except Exception as e:
        logger.error(f"[noVNC] WebSocket proxy error: {e}")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


# ==================== noVNC HTTP Proxy ====================

@router.get("/novnc")
@router.get("/novnc/{path:path}")
async def novnc_http_proxy(request: Request, path: str = "vnc.html"):
    """Proxy noVNC static files through the 5001 origin."""
    upstream_url = build_novnc_upstream_url(path, request.scope.get("query_string", b""))
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(upstream_url) as upstream:
                body = await upstream.read()
                return Response(
                    content=body,
                    status_code=upstream.status,
                    media_type=upstream.headers.get("content-type", "application/octet-stream"),
                    headers={"Cache-Control": "no-store"}
                )
    except aiohttp.ClientConnectorError:
        return error_response("noVNC 服务未运行，请先启动 VNC", status_code=503)
    except Exception as e:
        logger.error(f"[noVNC] HTTP proxy error: {e}")
        return error_response(f"noVNC 代理失败：{str(e)}", status_code=502)


# ==================== Host Validation ====================

@router.post("/api/desktop/validate")
async def validate_desktop_host(req: dict = Body(...)):
    """验证Ubuntu主机桌面连接并检查VNC服务"""
    try:
        host_connection = req.get('host', '')
        password = req.get('password', '')

        if not host_connection or '@' not in host_connection:
            return error_response('无效的主机格式 user@ip', 400)

        user, ip = CommonUtils.parse_host_address(host_connection)

        # 检查是否是本地主机
        is_local = CommonUtils.is_local_host(ip)

        if is_local:
            # 本地主机直接验证成功
            return JSONResponse(content={
                'success': True,
                'message': '本地主机验证成功',
                'needs_password': False,
                'local': True
            })

        # 远程主机验证
        ssh = None
        try:
            # 使用 ssh_manager 获取连接
            config = {
                'hostname': ip,
                'username': user,
                'password': password,
                'timeout': 10
            }
            ssh = ssh_manager.create_connection(config)

            # 如果密码认证失败，尝试密钥认证
            if not ssh:
                logger.info(f"[Desktop] Password auth failed for {user}@{ip}, trying key authentication")
                config['use_key_auth'] = True
                config.pop('password', None)  # Remove password for key auth
                ssh = ssh_manager.create_connection(config)

            if not ssh:
                return JSONResponse(
                    content={'success': False, 'error': 'SSH连接失败，请检查用户名、密码或SSH密钥配置', 'needs_password': True},
                    status_code=401
                )

            return JSONResponse(content={
                'success': True,
                'message': '主机验证成功',
                'needs_password': False,
                'password': password if password else ''
            })
        finally:
            # 确保SSH连接返回到连接池
            if ssh:
                try:
                    ssh_manager.return_connection(ssh)
                except Exception as e:
                    logger.warning(f"Failed to return SSH connection: {e}")

    except paramiko.AuthenticationException:
        return JSONResponse(
            content={'success': False, 'error': 'SSH认证失败', 'needs_password': True},
            status_code=401
        )
    except Exception as e:
        logger.error(f"Error validating host: {e}")
        return error_response(str(e), 500)


@router.post("/api/desktop/validate-host")
async def validate_desktop_host_legacy(req: dict = Body(...)):
    """/api/desktop/validate-host。"""
    return await validate_desktop_host(req)
