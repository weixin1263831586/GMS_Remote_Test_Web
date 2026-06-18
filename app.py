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
from contextlib import asynccontextmanager, suppress
from datetime import datetime

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware


# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.api_help import generate_help_or_continue
from core.clients import (
    get_client_id_from_request,
    get_client_ip,
    parse_client_id,
    probe_windows_usbipd,
    resolve_tailscale_device_host,
)
from core.config import config_manager
from core.notifications import safe_websocket_send, store_notification
from core.security_audit import classify_request_source
from core.security_audit_utils import should_audit_request, summarize_audit_request, summarize_audit_response
from core.settings import (
    APK_MAX_FILE_SIZE,
    APK_MAX_SOURCE_FILE_SIZE,
    APK_MAX_TASKS,
    APK_UPLOAD_DIR,
    CLEANUP_INTERVAL_SECONDS,
    DEVICE_CACHE_TTL,
    FORWARDED_ALLOW_IPS,
    GMS_ENV,
    GSI_PROGRESS_INCREMENT,
    GSI_PROGRESS_MAX,
    GSI_PROGRESS_POLL_INTERVAL,
    JADX_PATH,
    JADX_TIMEOUT,
    MAX_LOG_ENTRIES,
    PROJECT_ROOT,
    PROXY_HEADERS_ENABLED,
    SERVER_HOST,
    SERVER_PORT,
    _parse_csv_env,
)
from core.ssh import ssh_manager
from core.state import global_state
from features.devices.dependencies import configure_device_dependencies
from features.devices.monitor import (
    init_usb_monitor,
    invalidate_device_cache,
    start_usb_monitor,
    stop_usb_monitor,
)
from features.devices.network import run_local_shell_command
from features.devices.reconnect import stop_usbip_reconnect_tasks
from features.firmware.apk import (
    _cleanup_files,
    _create_apk_task,
    _normalize_apk_filename,
    _safe_join,
)
from features.firmware.dependencies import configure_firmware_dependencies
from features.redmine.api import redmine_service
from features.redmine.scheduler import start_redmine_agent_scheduler, stop_redmine_agent_scheduler
from features.test_execution.dependencies import (
    configure_test_execution_dependencies,
)
from modules.client_manager import client_manager
from routers import ALL_ROUTERS
from routers.system import init_templates
from workflows.device_test_execution import (
    acquire_test_devices,
    release_test_devices,
)
from workflows.firmware_device import (
    lock_firmware_devices,
    release_firmware_devices,
)


configure_firmware_dependencies(
    config_manager=config_manager,
    ssh_manager=ssh_manager,
    global_state=global_state,
    safe_websocket_send=safe_websocket_send,
    store_notification=store_notification,
    generate_help_or_continue=generate_help_or_continue,
    get_client_id_from_request=get_client_id_from_request,
    project_root=PROJECT_ROOT,
    apk_upload_dir=APK_UPLOAD_DIR,
    apk_max_tasks=APK_MAX_TASKS,
    apk_max_file_size=APK_MAX_FILE_SIZE,
    apk_max_source_file_size=APK_MAX_SOURCE_FILE_SIZE,
    jadx_path=JADX_PATH,
    jadx_timeout=JADX_TIMEOUT,
    gsi_progress_increment=GSI_PROGRESS_INCREMENT,
    gsi_progress_max=GSI_PROGRESS_MAX,
    gsi_progress_poll_interval=GSI_PROGRESS_POLL_INTERVAL,
    lock_firmware_devices=lock_firmware_devices,
    release_firmware_devices=release_firmware_devices,
)
configure_device_dependencies(
    ssh_manager=ssh_manager,
    config_manager=config_manager,
    global_state=global_state,
    store_notification=store_notification,
    generate_help_or_continue=generate_help_or_continue,
    get_client_id_from_request=get_client_id_from_request,
    probe_windows_usbipd=probe_windows_usbipd,
    resolve_tailscale_device_host=resolve_tailscale_device_host,
    safe_websocket_send=safe_websocket_send,
    get_client_ip=get_client_ip,
    client_manager=client_manager,
    run_local_shell_command=run_local_shell_command,
    project_root=PROJECT_ROOT,
    device_cache_ttl=DEVICE_CACHE_TTL,
)
configure_test_execution_dependencies(
    config_manager=config_manager,
    ssh_manager=ssh_manager,
    global_state=global_state,
    project_root=PROJECT_ROOT,
    safe_websocket_send=safe_websocket_send,
    generate_help_or_continue=generate_help_or_continue,
    get_client_id_from_request=get_client_id_from_request,
    parse_client_id=parse_client_id,
    store_notification=store_notification,
    apk_max_file_size=APK_MAX_FILE_SIZE,
    apk_upload_dir=APK_UPLOAD_DIR,
    max_log_entries=MAX_LOG_ENTRIES,
    create_apk_task=_create_apk_task,
    normalize_apk_filename=_normalize_apk_filename,
    safe_join=_safe_join,
    cleanup_files=_cleanup_files,
    acquire_test_devices=acquire_test_devices,
    release_test_devices=release_test_devices,
)

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


async def _periodic_cleanup():
    """Periodic state cleanup background task"""
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


def _start_usb_monitor(app):
    """Initialize USB monitor and return dispatch task (or None on failure)"""
    try:
        app.state.usb_event_queue = queue.Queue()

        from features.devices.manager import device_manager

        try:
            previous_devices = set(device_manager.get_connected_devices())
            logger.info(f"[USB Monitor] Initialized with devices: {previous_devices}")
            from features.devices.reconnect import (
                schedule_usbip_reconnect_for_missing_devices,
            )
            scheduled = schedule_usbip_reconnect_for_missing_devices(
                previous_devices,
                reason="startup persisted USB/IP source check",
            )
            if scheduled:
                logger.info("[USB Monitor] Startup scheduled USB/IP reconnect for hosts: %s", scheduled)
        except Exception as e:
            logger.error(f"[USB Monitor] Failed to get initial devices: {e}")
            previous_devices = set()

        def on_usb_devices_changed(devices):
            nonlocal previous_devices
            current_devices = set(devices)
            connected = list(current_devices - previous_devices)
            disconnected = list(previous_devices - current_devices)
            previous_devices = current_devices

            if disconnected:
                try:
                    from features.devices.reconnect import (
                        schedule_usbip_reconnect_for_removed_devices,
                    )
                    scheduled = schedule_usbip_reconnect_for_removed_devices(
                        disconnected,
                        reason="USB monitor detected disconnect",
                    )
                    if scheduled:
                        logger.info("[USB Monitor] Scheduled USB/IP reconnect for hosts: %s", scheduled)
                except Exception as e:
                    logger.error("[USB Monitor] Failed to schedule USB/IP reconnect: %s", e)

            invalidate_device_cache(global_state)
            app.state.usb_event_queue.put({
                'type': 'devices_changed',
                'devices': devices,
                'connected': connected,
                'disconnected': disconnected,
                'timestamp': datetime.now().isoformat()
            })

        init_usb_monitor(
            device_getter=lambda: device_manager.get_connected_devices(),
            on_devices_changed=on_usb_devices_changed,
            check_interval=2.0,
            use_udev=True,
        )
        start_usb_monitor()
        return asyncio.create_task(_dispatch_usb_events(app))
    except Exception as e:
        logger.error(f"Failed to start USB monitor: {e}")
        return None


async def _dispatch_usb_events(app):
    """Forward USB events to all connected WebSocket clients"""
    while True:
        try:
            event = app.state.usb_event_queue.get_nowait()
            from starlette.websockets import WebSocketState
            with global_state.websocket_connections_lock:
                clients = list(global_state.websocket_connections.items())

            await asyncio.gather(*(
                ws.send_json(event)
                for _, ws in clients
                if ws.client_state == WebSocketState.CONNECTED
            ))
        except queue.Empty:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"USB event dispatcher error: {e}")
            await asyncio.sleep(1)


async def _shutdown(cleanup_task, usb_dispatch_task, redmine_agent_scheduler_task):
    """Graceful shutdown of all background tasks"""
    logger.info("Application shutdown")
    cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup_task

    if usb_dispatch_task:
        usb_dispatch_task.cancel()
        with suppress(asyncio.CancelledError):
            await usb_dispatch_task

    if redmine_agent_scheduler_task:
        try:
            await stop_redmine_agent_scheduler()
        except Exception as e:
            logger.error(f"Error stopping RedmineAgent scheduler: {e}")

    try:
        stop_usbip_reconnect_tasks()
        stop_usb_monitor()
    except Exception as e:
        logger.error(f"Error stopping USB monitor: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("=" * 60)
    logger.info("Application startup")
    logger.info("=" * 60)

    cleanup_task = asyncio.create_task(_periodic_cleanup())
    redmine_agent_scheduler_task = start_redmine_agent_scheduler(redmine_service)

    # USB monitor
    usb_dispatch_task = _start_usb_monitor(app)

    yield

    # Shutdown
    await _shutdown(cleanup_task, usb_dispatch_task, redmine_agent_scheduler_task)


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
    from core.clients import get_client_id_from_request, get_client_ip, parse_client_id
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
            with suppress(Exception):
                response, response_summary = await summarize_audit_response(response)
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        if final_audit:
            try:
                client_id = get_client_id_from_request(request)
                username, client_ip = parse_client_id(client_id)
            except Exception:
                client_ip = get_client_ip(request)
                username = 'unknown'
                client_id = f'{username}@{client_ip}'
            with suppress(Exception):
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
