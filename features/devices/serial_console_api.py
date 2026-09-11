"""REST, WebSocket, and embedded page routes for local serial consoles."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from features.auth import (
    CurrentUser,
    require_agent_scope,
    require_authenticated_user_when_auth_required,
    require_permission_when_auth_required,
    validate_websocket_request,
)
from foundation.responses import error_response, success_response

from .serial_console import serial_console_service
from .serial_console_storage import DEFAULT_BAUDRATE


router = APIRouter(prefix="/api/devices/console", tags=["devices-console"])
page_router = APIRouter()


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
    Depends(require_permission_when_auth_required("devices.inventory"))
]


class BindingUpdate(BaseModel):
    label: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=500)
    baudrate: int = DEFAULT_BAUDRATE
    capture_enabled: bool = False
    newline: str = "cr"


def _service_error(exc: Exception):
    if isinstance(exc, ValueError):
        return error_response(str(exc), status_code=400)
    if isinstance(exc, KeyError):
        return error_response(str(exc.args[0]), status_code=404)
    return error_response(str(exc), status_code=409)


@page_router.get("/devices-console", response_class=HTMLResponse)
def devices_console_page(_user=Depends(require_authenticated_user_when_auth_required)):
    ui_dir = Path(__file__).with_name("ui")
    html = (ui_dir / "page.html").read_text(encoding="utf-8")
    html = html.replace(
        "{{CONSOLE_CSS}}", (ui_dir / "page.css").read_text(encoding="utf-8")
    )
    html = html.replace(
        "{{CONSOLE_JS}}", (ui_dir / "page.js").read_text(encoding="utf-8")
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/ports", dependencies=_READ_ACCESS)
def list_ports():
    try:
        ports = serial_console_service.list_ports()
    except Exception as exc:
        return _service_error(exc)
    return success_response(
        data={"ports": ports, "count": len(ports)}, message="串口列表已更新"
    )


@router.put("/bindings/{port_key}", dependencies=_WRITE_ACCESS)
def update_binding(port_key: str, body: BindingUpdate):
    try:
        binding = serial_console_service.update_binding(port_key, body.model_dump())
    except Exception as exc:
        return _service_error(exc)
    return success_response(
        data={"port_key": port_key, "binding": binding}, message="串口绑定已保存"
    )


@router.delete("/bindings/{port_key}", dependencies=_WRITE_ACCESS)
def delete_binding(port_key: str):
    try:
        deleted = serial_console_service.delete_binding(port_key)
    except Exception as exc:
        return _service_error(exc)
    if not deleted:
        return error_response("串口绑定不存在", status_code=404)
    return success_response(data={"port_key": port_key}, message="串口绑定已删除")


@router.post("/ports/{port_key}/capture/start", dependencies=_WRITE_ACCESS)
def start_capture(port_key: str):
    try:
        binding = serial_console_service.set_capture(port_key, True)
    except Exception as exc:
        return _service_error(exc)
    return success_response(data={"binding": binding}, message="常驻采集已启动")


@router.post("/ports/{port_key}/capture/stop", dependencies=_WRITE_ACCESS)
def stop_capture(port_key: str):
    try:
        binding = serial_console_service.set_capture(port_key, False)
    except Exception as exc:
        return _service_error(exc)
    return success_response(data={"binding": binding}, message="常驻采集已停止")


@router.get("/ports/{port_key}/logs", dependencies=_READ_ACCESS)
def read_logs(
    port_key: str,
    tail: int = Query(default=500, ge=1, le=10_000),
    date: str | None = Query(default=None),
):
    try:
        result = serial_console_service.read_log(port_key, date=date, tail=tail)
    except Exception as exc:
        return _service_error(exc)
    return success_response(data=result, message="日志已读取")


@router.get("/ports/{port_key}/logs/download", dependencies=_READ_ACCESS)
def download_log(port_key: str, date: str | None = Query(default=None)):
    try:
        path = serial_console_service.log_path(port_key, date)
    except Exception as exc:
        return _service_error(exc)
    if not path.is_file():
        return error_response("日志文件不存在", status_code=404)
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=f"{port_key}-{path.name}",
    )


@router.delete("/ports/{port_key}/logs", dependencies=_WRITE_ACCESS)
def clear_logs(port_key: str):
    try:
        deleted = serial_console_service.clear_logs(port_key)
    except Exception as exc:
        return _service_error(exc)
    return success_response(data={"deleted_files": deleted}, message="串口日志已清空")


async def _send_serial_data(websocket: WebSocket, queue: asyncio.Queue[str]) -> None:
    while True:
        await websocket.send_json({"type": "data", "data": await queue.get()})


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
    try:
        subscriber_id, queue, backlog = await serial_console_service.subscribe(port_key)
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "backlog",
                "data": backlog,
                "writable": _websocket_can_write(user),
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
                written = serial_console_service.write(
                    port_key,
                    str(message.get("data") or ""),
                    append_newline=bool(message.get("append_newline", False)),
                )
            except Exception as exc:
                await websocket.send_json({"type": "error", "error": str(exc)})
            else:
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
        if sender:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
        if subscriber_id:
            await asyncio.to_thread(
                serial_console_service.unsubscribe, port_key, subscriber_id
            )
