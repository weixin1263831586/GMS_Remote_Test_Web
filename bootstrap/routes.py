from __future__ import annotations

from fastapi import FastAPI

from features.assistant import api as assistant
from features.assistant.universal_ai import get_universal_analyzer
from features.automation import api as automation
from features.automation.api import configure_automation_service
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from features.devices import api as devices
from features.devices import config_explorer_api as device_config_explorer
from features.devices import get_or_create_user_state
from features.devices import integrations_api as device_integrations
from features.devices.dependencies import configure_device_dependencies
from features.devices.network import run_local_shell_command
from features.firmware import api as firmware
from features.firmware.apk import (
    _cleanup_files,
    _create_apk_task,
    _normalize_apk_filename,
    _safe_join,
)
from features.firmware.dependencies import configure_firmware_dependencies
from features.gerrit import api as gerrit_dashboard
from features.gerrit.dependencies import configure_redmine_users_provider
from features.gerrit.service import _query_gerrit_dual_mode
from features.redmine import api as redmine
from features.redmine.api import configure_redmine_service
from features.reports import api as reports
from features.reports.dependencies import configure_report_dependencies
from features.system import api as system
from features.system import assets, audit, desktop, integrations
from features.system import notifications_api as notifications
from features.system import terminal_api as terminal
from features.system.api import init_templates
from features.system.api_help import generate_help_or_continue
from features.system.mainline_issues import api as mainline_known_issues
from features.system.notifications import safe_websocket_send, store_notification
from features.system.ssh import ssh_manager
from features.system.ssh_async import ssh_async_manager
from features.system.state import global_state
from features.system.update_monitor import api as gms_update_monitor
from features.test_execution import api as tests
from features.test_execution.api import (
    _make_empty_suite_target,
    _resolve_suite_diagnosis_target,
)
from features.test_execution.dependencies import (
    configure_test_execution_dependencies,
)
from features.users import api as users
from features.users import (
    client_manager,
    get_client_id_from_request,
    get_client_ip,
    parse_client_id,
    probe_windows_usbipd,
    resolve_tailscale_device_host,
)
from features.users.dependencies import configure_user_dependencies
from foundation.config import (
    APK_MAX_FILE_SIZE,
    APK_MAX_SOURCE_FILE_SIZE,
    APK_MAX_TASKS,
    APK_UPLOAD_DIR,
    DEVICE_CACHE_TTL,
    GSI_PROGRESS_INCREMENT,
    GSI_PROGRESS_MAX,
    GSI_PROGRESS_POLL_INTERVAL,
    JADX_PATH,
    JADX_TIMEOUT,
    MAX_LOG_ENTRIES,
    PROJECT_ROOT,
    config_manager,
)
from foundation.files import FileUtils
from workflows.device_test_execution import (
    acquire_test_devices,
    release_test_devices,
)
from workflows.firmware_device import (
    lock_firmware_devices,
    release_firmware_devices,
)


ALL_ROUTERS = [
    assistant.router,
    assets.router,
    audit.router,
    automation.router,
    automation.page_router,
    desktop.router,
    devices.router,
    device_config_explorer.router,
    device_integrations.router,
    firmware.router,
    gerrit_dashboard.router,
    gerrit_dashboard.page_router,
    gms_update_monitor.router,
    gms_update_monitor.page_router,
    integrations.router,
    mainline_known_issues.router,
    notifications.router,
    redmine.router,
    redmine.page_router,
    reports.router,
    system.router,
    terminal.router,
    tests.router,
    users.router,
]


def include_routes(app: FastAPI, templates, services=None) -> None:
    if services is not None:
        configure_user_dependencies(
            config_manager=config_manager,
            global_state=global_state,
            ssh_async_manager=ssh_async_manager,
            get_or_create_user_state=get_or_create_user_state,
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
        device_config_explorer.configure_config_explorer_dependencies(
            generate_help_or_continue=generate_help_or_continue,
            create_apk_task=_create_apk_task,
            normalize_apk_filename=_normalize_apk_filename,
            safe_join=_safe_join,
            cleanup_files=_cleanup_files,
        )
        configure_report_dependencies(
            ssh_manager=ssh_manager,
            file_utils=FileUtils,
            universal_analyzer_factory=get_universal_analyzer,
            resolve_suite_target=_resolve_suite_diagnosis_target,
            make_empty_suite_target=_make_empty_suite_target,
        )
        configure_test_execution_dependencies(
            config_manager=config_manager,
            ssh_manager=ssh_manager,
            global_state=global_state,
            project_root=services.settings.project_root,
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
        configure_redmine_service(services.redmine)
        configure_redmine_users_provider(
            services.redmine.list_user_mappings,
        )
        profiles_path = (
            services.settings.project_root
            / 'configs/automation_profiles.json'
        )
        if not profiles_path.exists():
            profiles_path = (
                services.settings.project_root
                / 'configs/automation_profiles.example.json'
            )

        async def query_gerrit(query: str, limit: int):
            config = config_manager.get_gerrit_dashboard_config()
            effective_query = query.strip() or 'status:open'
            if 'limit:' not in effective_query:
                effective_query = f'{effective_query} limit:{limit}'
            result = await _query_gerrit_dual_mode(
                config,
                effective_query,
                max_changes=limit,
            )
            return result.get('items') or result.get('changes') or []

        configure_automation_service(
            AutomationService(
                store=AutomationStore(
                    services.settings.data_root
                    / 'automation/automation.sqlite3'
                ),
                profiles_path=profiles_path,
                gerrit_query=query_gerrit,
            )
        )
    init_templates(templates)
    for router in ALL_ROUTERS:
        app.include_router(router)
