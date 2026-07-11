from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from features.devices import get_or_create_user_state, get_usb_monitor
from foundation.responses import error_response

from . import runtime


logger = logging.getLogger(__name__)
router = APIRouter()


MODULE_LOG_PATTERNS = (
    re.compile(r"\b(?:CTS|VTS|GTS|STS|Tradefed|TradeFed|Compatibility Console|Invocation)\b"),
    re.compile(r"\b(?:ModuleListener|PrettyTestEventLogger|TestRunner|TestInvocation|ITestInvocationListener)\b"),
    re.compile(r"\b(?:testRunStarted|testRunEnded|testStarted|testEnded|testFailed|testIgnored|IGNORED|ASSUMPTION_FAILURE)\b"),
    re.compile(r"\b(?:PASSED|FAILED)\b"),
    re.compile(r"\[[0-9]+/[0-9]+\]\s+\S+\s+\S+#\S+"),
)


def _infer_log_source(message: str, source: object = None) -> str:
    if isinstance(source, str) and source in {"system", "module"}:
        return source
    return "module" if any(pattern.search(message) for pattern in MODULE_LOG_PATTERNS) else "system"


def _normalize_log_entry(entry: object) -> dict:
    """Normalize a stored log entry to the object shape consumed by the frontend.

    Newer entries are dicts {t, msg, type, source}; older sessions may still hold
    plain "string" entries - wrap those as system/info so the UI keeps working.
    """
    if isinstance(entry, dict):
        message = entry.get("msg") or entry.get("message") or ""
        return {
            "t": entry.get("t", ""),
            "msg": message,
            "type": entry.get("type", "info"),
            "source": _infer_log_source(str(message), entry.get("source")),
        }
    text = str(entry)
    if text.startswith("[") and "]" in text:
        prefix, _, rest = text.partition("]")
        message = rest.strip()
        return {"t": prefix.lstrip("["), "msg": message, "type": "info", "source": _infer_log_source(message)}
    return {"t": "", "msg": text, "type": "info", "source": _infer_log_source(text)}


# ==================== Test Status ====================

@router.get("/api/test/status")
async def get_status(
    request: Request,
    h: str | None = Query(None),
    help: bool = Query(False),
):
    """Get test status."""
    resp = runtime.generate_help_or_continue(help, "GET", "/api/test/status")
    if resp:
        return resp

    try:
        # Handle USB event queue if available
        try:
            import queue as _queue
            if hasattr(request.app.state, "usb_event_queue"):
                try:
                    while True:
                        event = request.app.state.usb_event_queue.get_nowait()

                        async def _send_usb_event(cid, ws, usb_event=event):
                            with contextlib.suppress(Exception):
                                await ws.send_json(usb_event)

                        await asyncio.gather(*[_send_usb_event(cid, ws) for cid, ws in list(runtime.global_state.websocket_connections.items())])
                except _queue.Empty:
                    pass
        except Exception:
            pass

        client_id = runtime.get_client_id_from_request(request)
        user_state = get_or_create_user_state(client_id)

        logger.info(f"[Status] Client {client_id} running={user_state.get('running', False)}")

        since = request.query_params.get("since")
        include_logs = request.query_params.get("logs", "true").lower() == "true"

        response = {
            "running": user_state.get("running", False),
            "devices": user_state.get("devices", []),
            "test_outcome": user_state.get("test_outcome", ""),
            "report_timestamp": user_state.get("report_timestamp", ""),
        }

        try:
            usb_monitor = get_usb_monitor()
            if usb_monitor:
                response["usb_monitor"] = {"mode": usb_monitor.mode, "running": usb_monitor.is_running, "pyudev_available": usb_monitor.pyudev_available}
        except Exception:
            pass

        if include_logs:
            logs = user_state.get("logs", [])
            normalized = [_normalize_log_entry(entry) for entry in logs]
            if since is not None and since.isdigit():
                since_int = int(since)
                if 0 <= since_int < len(normalized):
                    response["logs"] = normalized[since_int:]
                    response["log_count"] = len(normalized)
                else:
                    response["logs"] = normalized
                    response["log_count"] = len(normalized)
            else:
                response["logs"] = normalized
                response["log_count"] = len(normalized)

        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return error_response(str(e), status_code=500)


# ==================== Log Stream ====================

@router.get("/api/test/logs/stream")
async def stream_test_logs(request: Request):
    """Stream test logs (plain text format)."""
    client_id = runtime.get_client_id_from_request(request)

    async def log_stream():
        try:
            last_log_count = 0
            while True:
                user_state = get_or_create_user_state(client_id)
                running = user_state.get("running", False)
                logs = user_state.get("logs", [])
                current_log_count = len(logs)

                if current_log_count > last_log_count:
                    for i in range(last_log_count, current_log_count):
                        log_entry = _normalize_log_entry(logs[i])
                        yield f"{log_entry['t']} {log_entry['msg']}\n".strip() + "\n"
                    last_log_count = current_log_count

                if not running and last_log_count > 0:
                    yield "=== Test complete ===\n"
                    break

                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error in stream: {e}")
            yield f"Error: {e!s}\n"

    return StreamingResponse(
        log_stream(),
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "Access-Control-Allow-Origin": "*", "X-Accel-Buffering": "no"},
    )
