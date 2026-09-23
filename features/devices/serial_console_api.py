"""REST, WebSocket, and embedded page routes for local serial consoles."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from features.auth import (
    CurrentUser,
    require_agent_scope,
    require_authenticated_user_when_auth_required,
    require_human_principal_when_auth_required,
    require_permission_when_auth_required,
    validate_websocket_request,
)
from foundation.cluster_port import get_local_worker_id
from foundation.error_model import ApiError
from foundation.responses import success_response

from .serial_console import serial_console_service
from .serial_console_access import (
    SerialWriterClaim,
    acquire_writer_claim,
    audit_console_event,
    device_serial_availability,
    release_writer_claim,
    renew_writer_claim,
    require_visible_port,
    visible_ports,
)
from .serial_console_storage import DEFAULT_BAUDRATE


router = APIRouter(prefix="/api/devices/console", tags=["devices-console"])
page_router = APIRouter()
logger = logging.getLogger(__name__)


def _require_console_read(request: Request) -> CurrentUser | None:
    """Require devices.read for Agent Tokens while preserving human access."""

    user = require_authenticated_user_when_auth_required(request)
    if (
        user is not None
        and getattr(request.state, "auth_method", None) == "agent_token"
    ):
        return require_agent_scope("devices.read")(request)
    return user


_READ_ACCESS = [Depends(_require_console_read)]
_WRITE_ACCESS = [
    Depends(require_human_principal_when_auth_required),
    Depends(require_permission_when_auth_required("devices.inventory")),
]


class BindingUpdate(BaseModel):
    label: str = Field(default="", max_length=120)
    device_id: str = Field(default="", max_length=200)
    worker_id: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=500)
    baudrate: int = DEFAULT_BAUDRATE
    capture_enabled: bool = False
    newline: str = "cr"


def _service_error(exc: Exception):
    if isinstance(exc, ApiError):
        return exc.to_response()
    if isinstance(exc, ValueError):
        return ApiError.malformed_request(str(exc)).to_response()
    if isinstance(exc, KeyError):
        return ApiError.not_found(str(exc.args[0])).to_response()
    message = str(exc)
    if isinstance(exc, (OSError, PermissionError)) or any(
        marker in message for marker in ("pyserial", "权限不足", "枚举串口")
    ):
        return ApiError.dependency_unavailable(
            message,
            next_actions=(
                {"action": "检查 pyserial/pyudev、dialout 权限和串口设备节点"},
            ),
        ).to_response()
    return ApiError.conflict(message).to_response()


@page_router.get("/devices-console", response_class=HTMLResponse)
def devices_console_page(_user=Depends(require_authenticated_user_when_auth_required)):
    ui_dir = Path(__file__).with_name("ui")
    html = (ui_dir / "page.html").read_text(encoding="utf-8")
    html = html.replace(
        "{{CONSOLE_CSS}}", (ui_dir / "page.css").read_text(encoding="utf-8")
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@page_router.get("/devices-console/page.js")
def devices_console_page_js():
    """页面脚本走静态资源（CSP 收紧后禁止 inline <script>）。"""
    ui_dir = Path(__file__).with_name("ui")
    js = ui_dir / "page.js"
    return Response(
        js.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/ports", dependencies=_READ_ACCESS)
def list_ports(request: Request):
    try:
        ports = visible_ports(request, serial_console_service.list_ports())
    except Exception as exc:
        return _service_error(exc)
    return success_response(
        data={"ports": ports, "count": len(ports)}, message="串口列表已更新"
    )


@router.get("/availability/{device_id}", dependencies=_READ_ACCESS)
def serial_availability(
    request: Request,
    device_id: str,
    worker_id: str = Query(default=""),
):
    selected_worker = str(worker_id or get_local_worker_id()).strip()
    try:
        ports = visible_ports(request, serial_console_service.list_ports())
        result = device_serial_availability(
            ports, device_id=str(device_id or "").strip(), worker_id=selected_worker
        )
    except Exception as exc:
        return _service_error(exc)
    return success_response(data=result, message="设备串口可用性已评估")


@router.put("/bindings/{port_key}", dependencies=_WRITE_ACCESS)
def update_binding(request: Request, port_key: str, body: BindingUpdate):
    try:
        value = body.model_dump()
        if not value["device_id"]:
            return ApiError.invalid_semantics("绑定设备不能为空").to_response()
        if value["device_id"] and not value["worker_id"]:
            value["worker_id"] = get_local_worker_id()
        if value["worker_id"] != get_local_worker_id():
            return ApiError.invalid_semantics(
                "Controller 本机串口只能绑定到本机 Worker 设备"
            ).to_response()
        port = next(
            (
                item
                for item in serial_console_service.list_ports()
                if item.get("port_key") == port_key
            ),
            None,
        )
        if port is None:
            return ApiError.not_found("串口不存在").to_response()
        if port.get("identity_stable") is False:
            return ApiError.conflict(
                "该端口只有易变的 tty 名称，不能持久绑定；请配置 udev by-id 或稳定 USB 路径"
            ).to_response()
        binding = serial_console_service.update_binding(port_key, value)
    except Exception as exc:
        return _service_error(exc)
    audit_console_event(
        getattr(request.state, "current_user", None),
        "serial_binding_updated",
        {"port_key": port_key, "binding": binding},
        status="ok",
    )
    return success_response(
        data={"port_key": port_key, "binding": binding}, message="串口绑定已保存"
    )


@router.delete("/bindings/{port_key}", dependencies=_WRITE_ACCESS)
def delete_binding(request: Request, port_key: str):
    try:
        port = next(
            (item for item in serial_console_service.list_ports() if item.get("port_key") == port_key),
            {"port_key": port_key},
        )
        deleted = serial_console_service.delete_binding(port_key)
    except Exception as exc:
        return _service_error(exc)
    if not deleted:
        return ApiError.not_found("串口绑定不存在").to_response()
    audit_console_event(
        getattr(request.state, "current_user", None),
        "serial_binding_deleted",
        port,
        status="ok",
    )
    return success_response(data={"port_key": port_key}, message="串口绑定已删除")


@router.post("/ports/{port_key}/capture/start", dependencies=_WRITE_ACCESS)
def start_capture(request: Request, port_key: str):
    try:
        binding = serial_console_service.set_capture(port_key, True)
    except Exception as exc:
        return _service_error(exc)
    audit_console_event(
        getattr(request.state, "current_user", None), "serial_capture_started",
        {"port_key": port_key, "binding": binding}, status="ok")
    return success_response(data={"binding": binding}, message="常驻采集已启动")


@router.post("/ports/{port_key}/capture/stop", dependencies=_WRITE_ACCESS)
def stop_capture(request: Request, port_key: str):
    try:
        binding = serial_console_service.set_capture(port_key, False)
    except Exception as exc:
        return _service_error(exc)
    audit_console_event(
        getattr(request.state, "current_user", None), "serial_capture_stopped",
        {"port_key": port_key, "binding": binding}, status="ok")
    return success_response(data={"binding": binding}, message="常驻采集已停止")


def _capture_status(
    port_key: str, port: dict[str, Any] | None = None
) -> dict[str, Any]:
    """空日志时派生采集状态与下一步指引。

    list_ports() 已聚合 binding/online/capture_enabled/capture_active/
    error，这里直接复用；不改动 serial_console.py（其行数正卡在大小
    天花板上）。返回的 hint 用一句话回答「是没接线、没绑定、还是没开
    采集」，避免 agent/用户拿到空结果后无从排查。
    """
    port = port or next(
        (
            item
            for item in serial_console_service.list_ports()
            if item.get("port_key") == port_key
        ),
        None,
    )
    if port is None:
        return {
            "bound": False,
            "device_online": False,
            "capture_enabled": False,
            "capture_active": False,
            "hint": "该串口键未注册：确认设备已接入并在串口控制台页(/devices-console)重新扫描绑定。",
        }
    binding = port.get("binding")
    capture_enabled = bool(port.get("capture_enabled"))
    device_online = bool(port.get("online"))
    if not binding:
        hint = "该串口尚未绑定。在串口控制台页(/devices-console)选择设备并绑定后开启采集。"
    elif not device_online:
        hint = "串口已绑定但当前不在线(未插入或驱动未加载)，接好线后重试。"
    elif not capture_enabled:
        hint = "已绑定但常驻采集未开启：在串口控制台页对该端口开启「采集」，或 POST /api/devices/console/ports/{port_key}/capture/start。"
    elif not port.get("capture_active"):
        hint = str(port.get("error") or "采集已启用但串口尚未成功打开，请检查占用和 dialout 权限。")
    else:
        hint = "采集已开启但日志为空：确认目标端确有输出(u-boot/内核早期打印)，并核对绑定波特率与目标一致。"
    return {
        "bound": bool(binding),
        "device_online": device_online,
        "capture_enabled": capture_enabled,
        "capture_active": bool(port.get("capture_active")),
        "error": port.get("error"),
        "hint": hint,
    }


@router.get("/ports/{port_key}/logs", dependencies=_READ_ACCESS)
def read_logs(
    request: Request,
    port_key: str,
    tail: int = Query(default=500, ge=1, le=10_000),
    date: str | None = Query(default=None),
):
    try:
        port = require_visible_port(
            request, serial_console_service.list_ports(), port_key
        )
        result = serial_console_service.read_log(port_key, date=date, tail=tail)
    except Exception as exc:
        return _service_error(exc)
    if not result.get("lines"):
        # 空结果必须可定位原因：没接线、没绑定、没开采集。
        try:
            result["capture_status"] = _capture_status(port_key, port)
        except Exception:  # 状态派生失败不阻塞日志读取本身
            logger.debug("capture status derivation failed", exc_info=True)
    return success_response(data=result, message="日志已读取")


@router.get("/ports/{port_key}/logs/download", dependencies=_READ_ACCESS)
def download_log(
    request: Request, port_key: str, date: str | None = Query(default=None)
):
    try:
        require_visible_port(request, serial_console_service.list_ports(), port_key)
        path = serial_console_service.log_path(port_key, date)
    except Exception as exc:
        return _service_error(exc)
    if not path.is_file():
        return ApiError.not_found("日志文件不存在").to_response()
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=f"{port_key}-{path.name}",
    )


@router.delete("/ports/{port_key}/logs", dependencies=_WRITE_ACCESS)
def clear_logs(request: Request, port_key: str):
    try:
        port = next(
            (item for item in serial_console_service.list_ports() if item.get("port_key") == port_key),
            {"port_key": port_key},
        )
        deleted = serial_console_service.clear_logs(port_key)
    except Exception as exc:
        return _service_error(exc)
    audit_console_event(
        getattr(request.state, "current_user", None), "serial_logs_cleared",
        port, status="ok", byte_count=deleted)
    return success_response(data={"deleted_files": deleted}, message="串口日志已清空")


async def _send_serial_data(websocket: WebSocket, queue: asyncio.Queue[Any]) -> None:
    while True:
        data = await queue.get()
        if data is None:
            await websocket.close(code=4404, reason="串口绑定已删除")
            return
        await websocket.send_json({"type": "data", "data": data})


def _websocket_can_write(user: CurrentUser | None) -> bool:
    return user is None or user.has_permission("devices.inventory")


@router.websocket("/ws/{port_key}")
async def serial_console_websocket(websocket: WebSocket, port_key: str):
    user, close_code = validate_websocket_request(websocket)
    if close_code is not None:
        await websocket.close(code=close_code)
        return

    subscriber_id = ""
    sender: asyncio.Task | None = None
    writer_claim: SerialWriterClaim | None = None
    port: dict[str, Any] | None = None
    try:
        port = next(
            (
                item
                for item in serial_console_service.list_ports()
                if item.get("port_key") == port_key
            ),
            None,
        )
        if port is None:
            raise KeyError("串口不存在")
        subscriber_id, queue, backlog = await serial_console_service.subscribe(port_key)
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "backlog",
                "data": backlog,
                "writable": bool(
                    _websocket_can_write(user)
                    and (port.get("binding") or {}).get("identity_verified")
                ),
                "port_status": _capture_status(port_key, port),
            }
        )
        sender = asyncio.create_task(_send_serial_data(websocket, queue))
        while True:
            message = await websocket.receive_json()
            message_type = str(message.get("type") or "")
            if message_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if message_type != "input":
                await websocket.send_json(
                    {"type": "error", "error": "不支持的 WebSocket 消息类型"}
                )
                continue
            if not _websocket_can_write(user):
                await websocket.send_json({"type": "error", "error": "当前账号仅可查看串口"})
                continue
            try:
                latest = next(
                    (
                        item
                        for item in serial_console_service.list_ports()
                        if item.get("port_key") == port_key
                    ),
                    port,
                )
                port = latest
                if writer_claim is None:
                    writer_claim = acquire_writer_claim(user, latest)
                elif not renew_writer_claim(writer_claim):
                    release_writer_claim(writer_claim, user, latest)
                    writer_claim = acquire_writer_claim(user, latest)
                # pyserial write 最长阻塞 write_timeout(1s)（流控/驱动
                # 卡顿），放线程池避免冻结事件循环上的其它 WS/HTTP。
                written = await asyncio.to_thread(
                    serial_console_service.write,
                    port_key,
                    str(message.get("data") or ""),
                    append_newline=bool(message.get("append_newline", False)),
                )
            except Exception as exc:
                await websocket.send_json({"type": "error", "error": str(exc)})
            else:
                audit_console_event(
                    user, "serial_write", port, status="ok", byte_count=written
                )
                await websocket.send_json({"type": "written", "bytes": written})
    except KeyError as exc:
        if websocket.client_state.name != "CONNECTED":
            await websocket.close(code=4404, reason=str(exc.args[0]))
    except (ValueError, RuntimeError) as exc:
        if websocket.client_state.name != "CONNECTED":
            await websocket.close(code=4400, reason=str(exc)[:120])
        else:
            await websocket.send_json({"type": "error", "error": str(exc)})
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        release_writer_claim(writer_claim, user, port)
        if sender:
            sender.cancel()
            # 客户端断开后 sender 可能已因向关闭的 socket 写数据抛出
            # ConnectionClosed（非 CancelledError），不能让它逃逸成
            # ASGI 异常日志；其余异常也在此收敛，但留 debug 痕迹，
            # 避免真正的编程错误被无声吞掉。
            try:
                await sender
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug(
                    "sender teardown failed for %s", port_key, exc_info=True)
        if subscriber_id:
            await asyncio.to_thread(
                serial_console_service.unsubscribe, port_key, subscriber_id
            )
