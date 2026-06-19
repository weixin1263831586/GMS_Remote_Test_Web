from __future__ import annotations

import asyncio
import logging
import queue
from contextlib import asynccontextmanager, suppress
from datetime import datetime

from bootstrap.dependencies import AppServices
from foundation.config import CLEANUP_INTERVAL_SECONDS
from features.system.state import global_state
from features.devices.manager import device_manager
from features.devices.monitor import (
    init_usb_monitor,
    invalidate_device_cache,
    start_usb_monitor,
    stop_usb_monitor,
)
from features.devices.reconnect import (
    schedule_usbip_reconnect_for_missing_devices,
    schedule_usbip_reconnect_for_removed_devices,
    stop_usbip_reconnect_tasks,
)
from features.redmine.scheduler import (
    start_redmine_agent_scheduler,
    stop_redmine_agent_scheduler,
)


logger = logging.getLogger(__name__)


async def _periodic_cleanup() -> None:
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            global_state.cleanup_old_user_states()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Periodic cleanup failed')
            await asyncio.sleep(300)


async def _dispatch_usb_events(app) -> None:
    from starlette.websockets import WebSocketState

    while True:
        try:
            event = app.state.usb_event_queue.get_nowait()
            with global_state.websocket_connections_lock:
                clients = list(global_state.websocket_connections.values())
            await asyncio.gather(
                *(
                    websocket.send_json(event)
                    for websocket in clients
                    if websocket.client_state == WebSocketState.CONNECTED
                ),
                return_exceptions=True,
            )
        except queue.Empty:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('USB event dispatcher failed')
            await asyncio.sleep(1)


def _start_usb_monitor(app):
    app.state.usb_event_queue = queue.Queue()
    try:
        previous_devices = set(device_manager.get_connected_devices())
        schedule_usbip_reconnect_for_missing_devices(
            previous_devices,
            reason='startup persisted USB/IP source check',
        )
    except Exception:
        logger.exception('Failed to get initial devices')
        previous_devices = set()

    def on_usb_devices_changed(devices):
        nonlocal previous_devices
        current_devices = set(devices)
        connected = list(current_devices - previous_devices)
        disconnected = list(previous_devices - current_devices)
        previous_devices = current_devices
        if disconnected:
            schedule_usbip_reconnect_for_removed_devices(
                disconnected,
                reason='USB monitor detected disconnect',
            )
        invalidate_device_cache(global_state)
        app.state.usb_event_queue.put(
            {
                'type': 'devices_changed',
                'devices': devices,
                'connected': connected,
                'disconnected': disconnected,
                'timestamp': datetime.now().isoformat(),
            }
        )

    init_usb_monitor(
        device_getter=device_manager.get_connected_devices,
        on_devices_changed=on_usb_devices_changed,
        check_interval=2.0,
        use_udev=True,
    )
    start_usb_monitor()
    return asyncio.create_task(_dispatch_usb_events(app))


def create_lifespan(services: AppServices):
    @asynccontextmanager
    async def lifespan(app):
        app.state.services = services
        cleanup_task = asyncio.create_task(_periodic_cleanup())
        redmine_task = start_redmine_agent_scheduler(services.redmine)
        try:
            usb_dispatch_task = _start_usb_monitor(app)
        except Exception:
            logger.exception('Failed to start USB monitor')
            usb_dispatch_task = None
        yield
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        if usb_dispatch_task:
            usb_dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await usb_dispatch_task
        if redmine_task:
            await stop_redmine_agent_scheduler()
        stop_usbip_reconnect_tasks()
        stop_usb_monitor()

    return lifespan
