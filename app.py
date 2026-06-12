#!/usr/bin/env python3
"""
GMS Auto Test - FastAPI Application Entry Point (Port 5001)

模块化应用入口，路由和业务逻辑已拆分到 routers/ 和 core/ 目录
"""

import asyncio
import json
import logging
import os
import queue
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.settings import (
    CLEANUP_INTERVAL_SECONDS,
    FORWARDED_ALLOW_IPS,
    GMS_ENV,
    PROXY_HEADERS_ENABLED,
    SERVER_HOST,
    SERVER_PORT,
    _parse_csv_env,
)
from core.security_audit import classify_request_source
from core.security_audit_utils import should_audit_request, summarize_audit_request, summarize_audit_response
from core.state import global_state
from core.usb_monitor import init_usb_monitor, start_usb_monitor, stop_usb_monitor
from modules.redmine_agent_scheduler import start_redmine_agent_scheduler, stop_redmine_agent_scheduler

from routers import ALL_ROUTERS
from routers.system import init_templates

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Derived constants
CORS_ORIGINS = _parse_csv_env('CORS_ORIGINS', '*')
TRUSTED_HOSTS = _parse_csv_env('TRUSTED_HOSTS', '*')


class UTF8JSONResponse(JSONResponse):
    """自定义JSONResponse，确保UTF-8编码"""
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("=" * 60)
    logger.info("Application startup")
    logger.info("=" * 60)

    # Periodic cleanup
    async def periodic_cleanup():
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                global_state.cleanup_old_user_states()
                logger.info("Periodic cleanup completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup failed: {e}")
                await asyncio.sleep(300)

    cleanup_task = asyncio.create_task(periodic_cleanup())
    redmine_agent_scheduler_task = start_redmine_agent_scheduler()

    # USB monitor
    usb_dispatch_task = None
    try:
        if not hasattr(app.state, 'usb_event_queue'):
            app.state.usb_event_queue = queue.Queue()

        from core.devices import device_manager

        # Track previous device list for calculating connected/disconnected
        # Initialize with current devices to avoid false positives on startup
        try:
            previous_devices = set(device_manager.get_connected_devices())
            logger.info(f"[USB Monitor] Initialized with devices: {previous_devices}")
        except Exception as e:
            logger.error(f"[USB Monitor] Failed to get initial devices: {e}")
            previous_devices = set()

        def get_devices():
            try:
                return device_manager.get_connected_devices()
            except Exception as e:
                logger.error(f"Error getting devices: {e}")
                return []

        def on_usb_devices_changed(devices):
            nonlocal previous_devices
            current_devices = set(devices)

            # Calculate connected and disconnected devices
            connected = list(current_devices - previous_devices)
            disconnected = list(previous_devices - current_devices)

            # Update previous devices for next change
            previous_devices = current_devices

            with global_state.device_cache_lock:
                global_state.device_cache = {'devices': [], 'timestamp': 0}
            app.state.usb_event_queue.put({
                'type': 'devices_changed',
                'devices': devices,
                'connected': connected,
                'disconnected': disconnected,
                'timestamp': datetime.now().isoformat()
            })

        async def dispatch_usb_events():
            while True:
                try:
                    event = app.state.usb_event_queue.get_nowait()
                    from starlette.websockets import WebSocketState
                    with global_state.websocket_connections_lock:
                        clients = list(global_state.websocket_connections.items())

                    async def _send(cid, ws, usb_event=event):
                        if ws.client_state == WebSocketState.CONNECTED:
                            await ws.send_json(usb_event)

                    await asyncio.gather(*[_send(cid, ws) for cid, ws in clients])
                except queue.Empty:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"USB event dispatcher error: {e}")
                    await asyncio.sleep(1)

        init_usb_monitor(device_getter=get_devices, on_devices_changed=on_usb_devices_changed, check_interval=2.0, use_udev=True)
        start_usb_monitor()
        usb_dispatch_task = asyncio.create_task(dispatch_usb_events())
    except Exception as e:
        logger.error(f"Failed to start USB monitor: {e}")

    yield

    # Shutdown
    logger.info("Application shutdown")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    if usb_dispatch_task:
        usb_dispatch_task.cancel()
        try:
            await usb_dispatch_task
        except asyncio.CancelledError:
            pass

    if redmine_agent_scheduler_task:
        try:
            await stop_redmine_agent_scheduler()
        except Exception as e:
            logger.error(f"Error stopping RedmineAgent scheduler: {e}")

    try:
        stop_usb_monitor()
    except Exception as e:
        logger.error(f"Error stopping USB monitor: {e}")


# Create FastAPI app
app = FastAPI(
    title="GMS Auto Test - FastAPI Server (Port 5001)",
    description="完整的测试管理服务",
    version="4.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=os.getenv('CORS_ALLOW_CREDENTIALS', 'false').strip().lower() == 'true',
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
if TRUSTED_HOSTS != ['*']:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
app.add_middleware(GZipMiddleware, minimum_size=500)


# Audit middleware
@app.middleware("http")
async def add_headers_middleware(request, call_next):
    from core.clients import get_client_id_from_request, parse_client_id, get_client_ip
    from core.security_audit import security_audit_logger
    from core.security_audit_utils import can_audit_path, get_audit_operation

    path = request.url.path
    request_source = classify_request_source(request.headers.get('user-agent', ''), path)
    should_audit = should_audit_request(path, request_source, request.method)
    start_time = time.perf_counter()
    response = None
    error_text = None
    request_summary = {}
    response_summary = {}

    try:
        try:
            request_summary = await summarize_audit_request(request, should_audit)
        except Exception:
            request_summary = {'captured': False}
        response = await call_next(request)
    except Exception as e:
        error_text = str(e)
        raise
    finally:
        status_code = response.status_code if response else 500
        final_audit = should_audit or (status_code >= 400 and can_audit_path(path))
        if final_audit and response:
            try:
                response, response_summary = await summarize_audit_response(response)
            except Exception:
                pass
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        if final_audit:
            try:
                client_id = get_client_id_from_request(request)
                username, client_ip = parse_client_id(client_id)
            except Exception:
                client_ip = get_client_ip(request)
                username = 'unknown'
                client_id = f'{username}@{client_ip}'
            try:
                security_audit_logger.log_event({
                    'action_type': 'api' if path.startswith('/api/') else 'page_visit',
                    'source': request_source,
                    'operation': get_audit_operation(path, request.method),
                    'method': request.method,
                    'path': path,
                    'query': security_audit_logger.sanitize_mapping(dict(request.query_params)),
                    'request_summary': request_summary,
                    'response_summary': response_summary,
                    'status_code': status_code,
                    'duration_ms': duration_ms,
                    'client_ip': client_ip,
                    'client_id': client_id,
                    'username': username,
                    'user_agent': request.headers.get('user-agent', '')[:300],
                    'error': error_text,
                })
            except Exception:
                pass

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400, immutable"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# Static files and templates
static_dir = os.path.join(os.path.dirname(__file__), 'static')
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

templates_dir = os.path.join(os.path.dirname(__file__), 'templates')
templates = Jinja2Templates(directory=templates_dir)
templates.env.globals['url_for'] = lambda endpoint, filename='': f"/static/{filename}" if endpoint == 'static' else f'/{endpoint}'

# Initialize system router templates
init_templates(templates)

# Register all routers
for router in ALL_ROUTERS:
    app.include_router(router)


if __name__ == "__main__":
    logger.info("Starting GMS Auto Test FastAPI Server on port 5001...")
    uvicorn.run(
        "app:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level='info',
        proxy_headers=PROXY_HEADERS_ENABLED,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS if PROXY_HEADERS_ENABLED else "",
        timeout_keep_alive=120,
        loop='uvloop',
        http='httptools',
        access_log=(GMS_ENV != 'production'),
        limit_concurrency=500,
        limit_max_requests=10000,
    )
