from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
from contextlib import asynccontextmanager, suppress
from datetime import datetime

from bootstrap.dependencies import AppServices
from features.devices.manager import device_manager
from features.devices.monitor import (
    init_usb_monitor,
    invalidate_device_cache,
    start_usb_monitor,
    stop_usb_monitor,
)
from features.devices.reconnect import (
    filter_suppressed_usbip_devices,
    reconcile_observed_usbip_devices,
    schedule_usbip_reconnect_for_missing_devices,
    schedule_usbip_reconnect_for_removed_devices,
    stop_usbip_reconnect_tasks,
)
from features.redmine.scheduler import (
    start_redmine_agent_scheduler,
    stop_redmine_agent_scheduler,
)
from features.system.state import global_state
from foundation.config import CLEANUP_INTERVAL_SECONDS


logger = logging.getLogger(__name__)


def initialize_runtime_data(services: AppServices) -> None:
    data_root = services.settings.data_root
    data_root.mkdir(parents=True, exist_ok=True)

    # Redmine repository owns its schema, documents, and attachment workspace.
    services.redmine.repository.init_db()
    (data_root / 'redmine/attachments').mkdir(parents=True, exist_ok=True)

    from features.automation.repository import AutomationStore
    from features.reports.repository import TestReportDB
    from features.system.mainline_issues.repository import init_db as init_mainline_db
    from features.system.update_monitor.repository import init_db as init_update_monitor_db

    AutomationStore(data_root / 'automation/automation.sqlite3')
    TestReportDB(str(data_root / 'reports/test_reports.json'))

    update_monitor_db = data_root / 'gms_update_monitor.sqlite3'
    update_monitor_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(update_monitor_db) as conn:
        init_update_monitor_db(conn)

    mainline_db = data_root / 'mainline_known_issues.sqlite3'
    mainline_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(mainline_db) as conn:
        init_mainline_db(conn)


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
    state = {'previous_devices': set()}

    def on_usb_devices_changed(devices):
        reconcile_observed_usbip_devices(devices)
        visible_devices = filter_suppressed_usbip_devices(devices)
        current_devices = set(visible_devices)
        connected = list(current_devices - state['previous_devices'])
        disconnected = list(state['previous_devices'] - current_devices)
        state['previous_devices'] = current_devices
        if disconnected:
            schedule_usbip_reconnect_for_removed_devices(
                disconnected,
                reason='USB monitor detected disconnect',
            )
        invalidate_device_cache(global_state)
        app.state.usb_event_queue.put(
            {
                'type': 'devices_changed',
                'devices': visible_devices,
                'connected': connected,
                'disconnected': disconnected,
                'timestamp': datetime.now().isoformat(),
            }
        )

    def start_monitor_in_background():
        try:
            previous_devices = set(device_manager.get_connected_devices())
            state['previous_devices'] = previous_devices
            schedule_usbip_reconnect_for_missing_devices(
                previous_devices,
                reason='startup persisted USB/IP source check',
            )
        except Exception:
            logger.exception('Failed to get initial devices')

        try:
            init_usb_monitor(
                device_getter=device_manager.get_connected_devices,
                on_devices_changed=on_usb_devices_changed,
                check_interval=2.0,
                use_udev=True,
            )
            start_usb_monitor()
        except Exception:
            logger.exception('Failed to start USB monitor')

    threading.Thread(
        target=start_monitor_in_background,
        name='USBMonitor-Startup',
        daemon=True,
    ).start()
    return asyncio.create_task(_dispatch_usb_events(app))


def create_lifespan(services: AppServices):
    @asynccontextmanager
    async def lifespan(app):
        app.state.services = services
        initialize_runtime_data(services)
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
