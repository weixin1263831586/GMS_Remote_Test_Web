from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager, suppress
from datetime import datetime

from bootstrap.dependencies import AppServices
from features.cluster import get_cluster_service, start_local_bridge, stop_local_bridge
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
from features.system.notifications import bind_event_bus_loop, unbind_event_bus_loop
from features.system.state import global_state
from foundation.config import CLEANUP_INTERVAL_SECONDS
from foundation.controller_lock import controller_process_lock
from foundation.loop_watchdog import start_loop_watchdog, stop_loop_watchdog


logger = logging.getLogger(__name__)


def initialize_runtime_data(services: AppServices) -> None:
    data_root = services.settings.data_root
    data_root.mkdir(parents=True, exist_ok=True)

    # SQLite 不创建父目录，启动时统一补齐运行目录。
    for sub in (
        'apk_uploads', 'automation', 'build', 'cluster', 'cluster/artifacts',
        'cluster/artifact-uploads', 'cluster/transfers',
        'config_explorer_cache', 'gerrit', 'gerrit/by_user', 'knowledge',
        'knowledge/attachments', 'notes', 'notes/uploads', 'notifications',
        'redmine', 'redmine/attachments', 'redmine/by_user', 'redmine/docs',
        'reports', 'secrets', 'test_execution', 'uploads', 'uploads/gms_uploads',
        'user_prefs',
    ):
        (data_root / sub).mkdir(parents=True, exist_ok=True)

    # Redmine 仓库管理自身结构、文档和附件目录。
    services.redmine.repository.init_db()
    (data_root / 'redmine/attachments').mkdir(parents=True, exist_ok=True)

    from features.automation.repository import AutomationStore
    from features.cluster.repository import ClusterRepository
    from features.reports.repository import TestReportDB
    from features.system.mainline_issues.repository import init_db as init_mainline_db
    from features.system.update_monitor.repository import init_db as init_update_monitor_db

    AutomationStore(data_root / 'automation/automation.sqlite3')
    from features.cluster import ClusterConfig

    cluster_config = ClusterConfig.load()
    ClusterRepository(
        data_root / 'cluster/cluster.sqlite3',
        claim_lease_ttl_seconds=cluster_config.lease_ttl_seconds,
    )
    TestReportDB(str(data_root / 'reports/reports.sqlite3'))

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
            # 无 job 命令（device_action/flash）的实时日志事件只在
            # delete_job 清理，终态后仍会无限累积；按保留期定期裁剪。
            try:
                from features.cluster import get_cluster_service

                get_cluster_service().repository.prune_terminal_command_events()
            except Exception:
                logger.debug('command event prune skipped', exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Periodic cleanup failed')
            await asyncio.sleep(300)


async def _dispatch_usb_events(app) -> None:
    from starlette.websockets import WebSocketState

    while True:
        try:
            event = await app.state.usb_event_queue.get()
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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('USB event dispatcher failed')
            await asyncio.sleep(1)


def _start_local_vnc():
    """Start local x11vnc + websockify so the noVNC viewer is ready on boot."""

    def _start():
        try:
            from features.system.vnc import vnc_manager

            result = vnc_manager._start_local_vnc(force_restart=False)
            if result.get("success"):
                logger.info("[VNC] Auto-started local VNC on boot")
            else:
                logger.warning("[VNC] Local VNC auto-start skipped: %s",
                               result.get("error", "unknown"))
        except Exception:
            logger.exception("[VNC] Local VNC auto-start failed")

    threading.Thread(target=_start, name="VNC-AutoStart", daemon=True).start()


def _start_usb_monitor(app):
    loop = asyncio.get_running_loop()
    app.state.usb_event_queue = asyncio.Queue(maxsize=256)
    state = {'previous_devices': set()}

    def enqueue_usb_event(event):
        queue_ref = app.state.usb_event_queue
        if queue_ref.full():
            try:
                queue_ref.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue_ref.put_nowait(event)

    def submit_usb_event(event):
        try:
            loop.call_soon_threadsafe(enqueue_usb_event, event)
        except RuntimeError:
            logger.debug('USB event dropped because the application loop is closed')

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
        submit_usb_event(
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
        with controller_process_lock(services.settings.data_root):
            app.state.services = services
            initialize_runtime_data(services)
            cluster = get_cluster_service()
            start_local_bridge(cluster.repository, cluster.config)
            event_loop = bind_event_bus_loop()
            cleanup_task = asyncio.create_task(_periodic_cleanup())
            app.state.cleanup_task = cleanup_task
            # 事件循环卡死观测：卡死 >30s 时由 C 级定时器 dump 全部线程栈
            # （见 foundation/loop_watchdog.py 的模块注释）。
            loop_watchdog_task = await start_loop_watchdog()
            app.state.loop_watchdog_task = loop_watchdog_task
            redmine_task = start_redmine_agent_scheduler(services.redmine)
            app.state.redmine_scheduler_task = redmine_task
            from features.system.desktop import create_novnc_client_session

            novnc_session = create_novnc_client_session()
            app.state.novnc_client_session = novnc_session
            try:
                _start_local_vnc()
            except Exception:
                logger.exception('Failed to auto-start local VNC')
            try:
                from foundation.static_routes import apply_static_routes_async

                apply_static_routes_async()
            except Exception:
                logger.exception('Failed to apply static routes')
            try:
                usb_dispatch_task = _start_usb_monitor(app)
            except Exception:
                logger.exception('Failed to start USB monitor')
                usb_dispatch_task = None
            app.state.usb_dispatch_task = usb_dispatch_task
            automation_task = None
            try:
                from features.automation.worker import start_automation_worker

                automation_task = start_automation_worker()
            except Exception:
                logger.exception('Failed to start automation worker')
            app.state.automation_task = automation_task
            from features.firmware.apk import recover_apk_analysis_tasks

            app.state.apk_recovery_tasks = recover_apk_analysis_tasks()
            from features.test_execution.transfers_api import recover_suite_tasks

            app.state.suite_recovery_tasks = recover_suite_tasks()
            try:
                yield
            finally:
                await stop_loop_watchdog(loop_watchdog_task)
                unbind_event_bus_loop(event_loop)
                background_tasks = list(global_state.background_tasks)
                for task in background_tasks:
                    task.cancel()
                if background_tasks:
                    await asyncio.gather(
                        *background_tasks,
                        return_exceptions=True,
                    )
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup_task
                if usb_dispatch_task:
                    usb_dispatch_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await usb_dispatch_task
                if automation_task:
                    from features.automation.worker import stop_automation_worker

                    with suppress(asyncio.CancelledError):
                        await stop_automation_worker()
                if redmine_task:
                    await stop_redmine_agent_scheduler()
                try:
                    stop_local_bridge()
                except Exception:
                    pass
                stop_usbip_reconnect_tasks()
                stop_usb_monitor()
                await novnc_session.close()
                app.state.novnc_client_session = None

    return lifespan
