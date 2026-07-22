"""Desktop/VNC routes - VNC status, start, stop, noVNC proxy, host validation."""

import asyncio
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode

import aiohttp
from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from features.auth import (
    AUTH_COOKIE_NAME,
    CurrentUser,
    auth_service,
    require_elevated_admin,
    validate_websocket_request,
)
from features.system.models import VNCStartRequest
from features.system.novnc_access import novnc_access_service
from features.system.vnc import NOVNC_WEB_PORT, vnc_manager
from foundation.config import config_manager
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)

router = APIRouter()

# 短暂缓存 VNC 状态；启动和停止操作会立即使缓存失效。
_VNC_STATUS_TTL = 10.0
_vnc_status_cache: dict[str, float] = {"ts": 0.0, "value": None}
_NOVNC_ZH_CN_LOCALE = Path(__file__).with_name("novnc_zh_cn.json")
_NOVNC_ASSET_VERSION = "20260718-clipboard-focus"


def novnc_locale_override(path: str) -> bytes | None:
    """Return the platform's Simplified Chinese noVNC locale when requested."""
    if path.lstrip("/") != "app/locale/zh.json":
        return None
    return _NOVNC_ZH_CN_LOCALE.read_bytes()


def novnc_asset_override(path: str, body: bytes) -> bytes:
    """抑制未聚焦 noVNC iframe 的剪贴板写入错误并刷新资源版本。"""
    normalized = path.lstrip("/") or "vnc.html"
    version = _NOVNC_ASSET_VERSION.encode()
    if normalized == "vnc.html":
        return body.replace(
            b'from "./app/ui.js";',
            b'from "./app/ui.js?gms_asset=' + version + b'";',
            1,
        )
    if normalized == "app/ui.js":
        return body.replace(
            b'from "../core/rfb.js";',
            b'from "../core/rfb.js?gms_asset=' + version + b'";',
            1,
        )
    if normalized == "core/rfb.js":
        return body.replace(
            b'from "./clipboard.js";',
            b'from "./clipboard.js?gms_asset=' + version + b'";',
            1,
        )
    if normalized == "core/clipboard.js":
        return body.replace(
            b"if (!this._isAvailable) return false;",
            b"if (!this._isAvailable || !document.hasFocus()) return false;",
            1,
        )
    return body


def _invalidate_vnc_status_cache() -> None:
    _vnc_status_cache["ts"] = 0.0
    _vnc_status_cache["value"] = None


def _authorized_vnc_target(worker_id: str = "") -> tuple[str, str, str]:
    """Return host connection, SSH password, and VNC password from server state."""

    from features.cluster import get_cluster_service

    config = config_manager.load_config()
    cluster = get_cluster_service()
    normalized = str(worker_id or "").strip() or cluster.config.local_worker_id
    if normalized == cluster.config.local_worker_id:
        host = (
            f"{config_manager.get_ubuntu_user(config)}@"
            f"{config_manager.get_ubuntu_host(config) or 'localhost'}"
        )
        return host, str(config.get("ubuntu_pswd") or ""), str(config.get("vnc_password") or "")

    worker = cluster.repository.get_worker(normalized)
    if not worker or worker.get("status") not in {"online", "busy", "draining"}:
        raise ValueError("所选 Worker 不在线")
    address = str(worker.get("address") or worker.get("hostname") or "").strip()
    user = str((worker.get("capabilities") or {}).get("ssh_user") or "").strip()
    if not address or not user:
        raise ValueError("Worker 缺少 SSH 连接元数据")
    password = config_manager.find_device_host_password(f"{user}@{address}", config) or ""
    return f"{user}@{address}", password, ""

def default_novnc_upstream_http() -> str:
    return f"http://127.0.0.1:{NOVNC_WEB_PORT}"


# noVNC upstream URLs
NOVNC_UPSTREAM_HTTP = os.getenv("GMS_NOVNC_UPSTREAM", default_novnc_upstream_http()).rstrip("/")
NOVNC_UPSTREAM_WS = NOVNC_UPSTREAM_HTTP.replace("http://", "ws://", 1).replace("https://", "wss://", 1)


def _cluster_novnc_upstream(worker_id: str) -> str:
    from features.cluster import get_cluster_service

    cluster = get_cluster_service()
    worker = cluster.repository.get_worker(worker_id)
    if not cluster.effective_enabled or not worker:
        raise ValueError("Worker 不存在或集群模式未启用")
    if worker.get("status") not in {"online", "busy", "draining"}:
        raise ValueError("Worker 不在线")
    address = str(worker.get("address") or worker.get("hostname") or "").strip()
    if not address:
        raise ValueError("Worker 缺少桌面地址")
    port = int((worker.get("capabilities") or {}).get("novnc_port") or NOVNC_WEB_PORT)
    return f"http://{address}:{port}"


def _upstream_query_string(query_string: bytes) -> str:
    values = parse_qsl(
        query_string.decode("utf-8", errors="ignore"),
        keep_blank_values=True,
    )
    return urlencode(
        [(key, value) for key, value in values if key != "access_token"]
    )


def build_novnc_upstream_url(path: str, query_string: bytes = b"") -> str:
    upstream_path = path.lstrip("/") or "vnc.html"
    url = f"{NOVNC_UPSTREAM_HTTP}/{upstream_path}"
    upstream_query = _upstream_query_string(query_string)
    if upstream_query:
        url = f"{url}?{upstream_query}"
    return url


def _novnc_worker_scope(worker_id: str = "") -> str:
    from features.cluster import get_cluster_service

    cluster = get_cluster_service()
    normalized = str(worker_id or "").strip() or cluster.config.local_worker_id
    if normalized != cluster.config.local_worker_id:
        _cluster_novnc_upstream(normalized)
    return normalized


def _valid_novnc_access(
    connection: Request | WebSocket,
    user: CurrentUser | None,
    worker_id: str,
) -> bool:
    if user is None or not auth_service.get_elevated_until(
        str(connection.cookies.get(AUTH_COOKIE_NAME) or "")
    ):
        return False
    return novnc_access_service.validate(
        str(connection.query_params.get("access_token") or ""),
        user,
        str(connection.cookies.get(AUTH_COOKIE_NAME) or ""),
        worker_id,
    )


def _novnc_entry_path(path: str) -> bool:
    return path.lstrip("/") in {"", "vnc.html"}


def _requested_novnc_subprotocol(websocket: WebSocket) -> str | None:
    requested = {
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    }
    return "binary" if "binary" in requested else None


async def _relay_novnc_websockets(websocket: WebSocket, upstream) -> None:
    """Relay noVNC frames and finish the downstream socket gracefully."""

    async def client_to_upstream():
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                await upstream.close()
                return "client"
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
            elif message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break
        return "upstream"

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    results = await asyncio.gather(*done)
    if "upstream" in results:
        close_code = getattr(upstream, "close_code", None)
        if not isinstance(close_code, int) or close_code in {1005, 1006}:
            close_code = 1000
        with suppress(Exception):
            await websocket.close(code=close_code)


# ==================== VNC Routes ====================

@router.post("/api/desktop/novnc/access")
async def create_novnc_access(
    request: Request,
    req: dict | None = Body(default=None),
    admin: CurrentUser = Depends(require_elevated_admin),
):
    """Issue a short-lived grant bound to this admin session and one Worker."""

    try:
        worker_id = _novnc_worker_scope(str((req or {}).get("worker_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session_token = str(request.cookies.get(AUTH_COOKIE_NAME) or "")
    try:
        access_token, expires_at = novnc_access_service.issue(
            admin,
            session_token,
            worker_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    from features.cluster import get_cluster_service

    local_worker_id = get_cluster_service().config.local_worker_id
    if worker_id == local_worker_id:
        socket_path = f"websockify?access_token={quote(access_token)}"
        entry_path = "/novnc/vnc.html"
    else:
        encoded_worker = quote(worker_id, safe="")
        socket_path = (
            f"cluster/novnc/{encoded_worker}/websockify"
            f"?access_token={quote(access_token)}"
        )
        entry_path = f"/cluster/novnc/{encoded_worker}/vnc.html"
    query = urlencode(
        {
            "autoconnect": "true",
            "resize": "scale",
            "path": socket_path,
            "access_token": access_token,
        }
    )
    return {
        "success": True,
        "worker_id": worker_id,
        "expires_at": expires_at,
        "url": f"{entry_path}?{query}",
    }

@router.get("/api/desktop/vnc/status")
async def get_desktop_vnc_status(
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """获取VNC状态"""
    now = time.monotonic()
    if _vnc_status_cache["value"] is not None and now - _vnc_status_cache["ts"] < _VNC_STATUS_TTL:
        return success_response(_vnc_status_cache["value"])
    try:
        result = await asyncio.to_thread(vnc_manager.get_vnc_status)
        _vnc_status_cache["ts"] = now
        _vnc_status_cache["value"] = result
        return success_response(result)
    except Exception as e:
        logger.error(f"Error getting VNC status: {e}")
        return error_response(f"{e!s}. 请检查配置和参数是否正确。", status_code=500)


@router.post("/api/desktop/vnc/start")
async def start_desktop_vnc(
    req: VNCStartRequest | None = Body(default=None),
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """启动Ubuntu主机桌面VNC（Ubuntu桌面的VNC服务）"""
    from features.cluster import get_cluster_service

    cluster = get_cluster_service()
    requested_worker = str(req.worker_id if req else "").strip() or cluster.config.local_worker_id
    if requested_worker != cluster.config.local_worker_id:
        return error_response(
            "远端桌面只能通过 Worker Agent 的 restart-vnc 命令管理",
            status_code=409,
        )
    try:
        host, password, vnc_password = _authorized_vnc_target(requested_worker)
    except ValueError as exc:
        return error_response(str(exc), status_code=409)
    force_restart = bool(req and req.force_restart)

    # VNC 启动包含阻塞式 SSH 调用，在线程中执行。
    result = await asyncio.to_thread(vnc_manager.start_vnc, host, password, vnc_password, force_restart=force_restart)
    _invalidate_vnc_status_cache()
    return JSONResponse(content=result)


@router.post("/api/desktop/vnc/stop")
async def stop_desktop_vnc(
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """停止Ubuntu主机桌面VNC"""
    result = await asyncio.to_thread(vnc_manager.stop_vnc)
    _invalidate_vnc_status_cache()
    return JSONResponse(content=result)


# ==================== noVNC WebSocket Proxy ====================

@router.websocket("/websockify")
@router.websocket("/novnc/websockify")
@router.websocket("/novnc/novnc/websockify")
async def novnc_websockify_proxy(websocket: WebSocket):
    """Proxy noVNC websocket through the 5001 origin for Tailscale/remote access."""
    user, close_code = validate_websocket_request(websocket)
    if close_code:
        await websocket.close(code=close_code)
        return
    worker_id = _novnc_worker_scope()
    if not _valid_novnc_access(websocket, user, worker_id):
        await websocket.close(code=4403)
        return
    subprotocol = _requested_novnc_subprotocol(websocket)
    await websocket.accept(subprotocol=subprotocol)
    upstream_url = f"{NOVNC_UPSTREAM_WS}/websockify"

    try:
        protocols = (subprotocol,) if subprotocol else ()
        async with aiohttp.ClientSession() as session, session.ws_connect(
            upstream_url, protocols=protocols
        ) as upstream:
            await _relay_novnc_websockets(websocket, upstream)
    except Exception as e:
        logger.error(f"[noVNC] WebSocket proxy error: {e}")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


@router.websocket("/cluster/novnc/{worker_id}/websockify")
async def cluster_novnc_websockify_proxy(websocket: WebSocket, worker_id: str):
    """Proxy a Worker's noVNC socket through the Controller HTTPS origin."""
    user, close_code = validate_websocket_request(websocket)
    if close_code:
        await websocket.close(code=close_code)
        return
    try:
        worker_id = _novnc_worker_scope(worker_id)
    except ValueError:
        await websocket.close(code=4404)
        return
    if not _valid_novnc_access(websocket, user, worker_id):
        await websocket.close(code=4403)
        return
    subprotocol = _requested_novnc_subprotocol(websocket)
    await websocket.accept(subprotocol=subprotocol)
    try:
        upstream_http = _cluster_novnc_upstream(worker_id)
        from features.cluster import worker_tokens

        worker_token = worker_tokens().get(worker_id, "")
        if not worker_token:
            raise RuntimeError("Worker noVNC token is not configured")
        upstream_url = (
            upstream_http.replace("http://", "ws://", 1)
            + "/websockify?token="
            + quote(worker_token, safe="")
        )
        protocols = (subprotocol,) if subprotocol else ()
        async with aiohttp.ClientSession() as session, session.ws_connect(
            upstream_url, protocols=protocols
        ) as upstream:
            await _relay_novnc_websockets(websocket, upstream)
    except Exception as exc:
        logger.error("[noVNC] Worker %s WebSocket proxy error: %s", worker_id, exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


# ==================== noVNC HTTP Proxy ====================

@router.get("/novnc")
@router.get("/novnc/{path:path}")
async def novnc_http_proxy(
    request: Request,
    path: str = "vnc.html",
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """Proxy noVNC static files through the 5001 origin."""
    worker_id = _novnc_worker_scope()
    if _novnc_entry_path(path) and not _valid_novnc_access(request, _admin, worker_id):
        raise HTTPException(status_code=403, detail="noVNC access grant required")
    locale = novnc_locale_override(path)
    if locale is not None:
        return Response(content=locale, media_type="application/json", headers={"Cache-Control": "no-cache"})
    upstream_url = build_novnc_upstream_url(path, request.scope.get("query_string", b""))
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(upstream_url) as upstream:
            body = await upstream.read()
            body = novnc_asset_override(path, body)
            # 静态资源缓存一天；入口 HTML/JSON 每次请求重新验证。
            cache_control = (
                "no-cache"
                if path.endswith(('.html', '.json')) or path in {'', 'vnc.html'}
                else "public, max-age=86400"
            )
            return Response(
                content=body,
                status_code=upstream.status,
                media_type=upstream.headers.get("content-type", "application/octet-stream"),
                headers={"Cache-Control": cache_control}
            )
    except aiohttp.ClientConnectorError:
        return error_response("noVNC 服务未运行，请先启动 VNC", status_code=503)
    except Exception as e:
        logger.error(f"[noVNC] HTTP proxy error: {e}")
        return error_response(f"noVNC 代理失败：{e!s}", status_code=502)


@router.get("/cluster/novnc/{worker_id}")
@router.get("/cluster/novnc/{worker_id}/{path:path}")
async def cluster_novnc_http_proxy(
    request: Request,
    worker_id: str,
    path: str = "vnc.html",
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """Serve Worker noVNC assets without mixed-content or browser routing issues."""
    try:
        worker_id = _novnc_worker_scope(worker_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=409)
    if _novnc_entry_path(path) and not _valid_novnc_access(request, _admin, worker_id):
        raise HTTPException(status_code=403, detail="noVNC access grant required")
    locale = novnc_locale_override(path)
    if locale is not None:
        return Response(content=locale, media_type="application/json", headers={"Cache-Control": "no-cache"})
    try:
        upstream = _cluster_novnc_upstream(worker_id)
        upstream_path = path.lstrip("/") or "vnc.html"
        url = f"{upstream}/{upstream_path}"
        upstream_query = _upstream_query_string(request.scope.get("query_string", b""))
        if upstream_query:
            url += "?" + upstream_query
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
            body = await response.read()
            body = novnc_asset_override(upstream_path, body)
            cache = "no-cache" if upstream_path.endswith((".html", ".json")) else "public, max-age=86400"
            return Response(content=body, status_code=response.status,
                            media_type=response.headers.get("content-type", "application/octet-stream"),
                            headers={"Cache-Control": cache})
    except ValueError as exc:
        return error_response(str(exc), status_code=409)
    except Exception as exc:
        logger.error("[noVNC] Worker %s HTTP proxy error: %s", worker_id, exc)
        return error_response(f"Worker noVNC 代理失败：{exc}", status_code=502)


# ==================== Host Validation ====================

@router.post("/api/desktop/validate")
async def validate_desktop_host(
    req: dict = Body(...),
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """Reject browser-supplied targets; hosts must be registered as Workers."""
    return error_response(
        "不再接受客户端提交的主机或密码；请先将主机注册为受管 Worker",
        status_code=410,
    )
