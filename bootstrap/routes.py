from __future__ import annotations

from fastapi import FastAPI

from features import knowledge
from features.assistant import api as assistant
from features.assistant.universal_ai import get_universal_analyzer
from features.auth import router as auth_router
from features.auth.api import configure_client_ssh_authenticator
from features.automation import api as automation
from features.automation.api import configure_automation_service
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from features.build import api as build
from features.build.api import configure_build_service
from features.build.repository import BuildStore
from features.build.service import BuildService
from features.cluster import api as cluster
from features.cluster import get_cluster_service
from features.devices import api as devices
from features.devices import config_explorer_api as device_config_explorer
from features.devices import config_override_api as device_config_override
from features.devices import integrations_api as device_integrations
from features.devices.dependencies import configure_device_dependencies
from features.devices.network import run_local_shell_command
from features.devices.support import get_or_create_user_state
from features.email import api as email
from features.firmware import api as firmware
from features.firmware.apk import (
    _cleanup_files,
    _create_apk_task,
    _normalize_apk_filename,
    _safe_join,
)
from features.firmware.dependencies import configure_firmware_dependencies
from features.gerrit import api as gerrit_dashboard
from features.gerrit.config import (
    denormalize_gerrit_dashboard_config,
    normalize_gerrit_dashboard_config,
)
from features.gerrit.service import _query_gerrit_dual_mode
from features.gerrit.settings import config_manager as gerrit_config_manager
from features.redmine import api as redmine
from features.redmine import reply_api as redmine_reply
from features.redmine.api import configure_redmine_service
from features.redmine.dashboard import (
    denormalize_redmine_dashboard_config,
    normalize_redmine_dashboard_profiles,
    normalize_redmine_stats_config,
)
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
from features.test_execution.suite_task_store import SuiteTaskStore
from features.users import api as users
from features.users import (
    client_manager,
    get_client_id_from_request,
    get_client_ip,
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
from workflows.cluster_test_execution import start_cluster_test
from workflows.firmware_device import (
    lock_firmware_devices,
    release_firmware_devices,
)


ALL_ROUTERS = [
    assistant.router,
    assets.router,
    audit.router,
    auth_router,
    automation.router,
    automation.page_router,
    build.router,
    cluster.router,
    cluster.page_router,
    desktop.router,
    devices.router,
    device_config_explorer.router,
    device_config_override.router,
    device_integrations.router,
    email.router,
    firmware.router,
    gerrit_dashboard.router,
    gerrit_dashboard.page_router,
    gms_update_monitor.router,
    gms_update_monitor.page_router,
    integrations.router,
    mainline_known_issues.router,
    notifications.router,
    knowledge.router,
    knowledge.page_router,
    redmine.router,
    redmine.page_router,
    redmine_reply.router,
    reports.router,
    system.router,
    terminal.router,
    tests.router,
    users.router,
]


def configure_config_sections() -> None:
    config_manager.configure_section_normalizer(
        'redmine_stats',
        normalizer=normalize_redmine_stats_config,
        denormalizer=normalize_redmine_stats_config,
    )
    config_manager.configure_section_normalizer(
        'redmine_dashboard',
        normalizer=normalize_redmine_dashboard_profiles,
        denormalizer=denormalize_redmine_dashboard_config,
    )
    config_manager.configure_section_normalizer(
        'gerrit_dashboard',
        normalizer=normalize_gerrit_dashboard_config,
        denormalizer=denormalize_gerrit_dashboard_config,
    )


def _build_device_components():
    """Return ``(device_selector, device_manager)`` from device globals.

    Both may be None if the device singletons are unavailable, in which case
    the executor falls back to manual device selection and skips post-flash
    property verification.
    """
    try:
        from features.automation.device_selector import DeviceSelector
        from features.devices import device_lock_manager
        from features.devices.manager import device_manager

        return DeviceSelector(device_manager, device_lock_manager), device_manager
    except Exception:
        return None, None


def include_routes(app: FastAPI, templates, services=None) -> None:
    if services is not None:
        cluster.configure_cluster(services.settings.data_root)
        configure_config_sections()
        configure_user_dependencies(
            config_manager=config_manager,
            data_root=services.settings.data_root,
            global_state=global_state,
            ssh_async_manager=ssh_async_manager,
            get_or_create_user_state=get_or_create_user_state,
        )
        configure_client_ssh_authenticator(client_manager.detect_username)
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
            apk_max_file_size=APK_MAX_FILE_SIZE,
            apk_upload_dir=APK_UPLOAD_DIR,
            max_log_entries=MAX_LOG_ENTRIES,
            create_apk_task=_create_apk_task,
            normalize_apk_filename=_normalize_apk_filename,
            safe_join=_safe_join,
            cleanup_files=_cleanup_files,
            start_cluster_test=start_cluster_test,
            suite_task_store=SuiteTaskStore(
                services.settings.data_root
                / "test_execution/suite_tasks.sqlite3"
            ),
        )
        configure_redmine_service(services.redmine)
        from foundation.config_paths import automation_profiles_path, build_servers_path

        profiles_path = automation_profiles_path(services.settings.project_root)

        async def query_gerrit(owner_id: str, query: str, limit: int):
            config = gerrit_config_manager.for_owner(
                owner_id
            ).get_gerrit_dashboard_config()
            effective_query = query.strip() or 'status:open'
            if 'limit:' not in effective_query:
                effective_query = f'{effective_query} limit:{limit}'
            result = await _query_gerrit_dual_mode(
                config,
                effective_query,
                max_changes=limit,
            )
            return result.get('items') or result.get('changes') or []

        device_selector, device_manager = _build_device_components()
        configure_automation_service(
            AutomationService(
                store=AutomationStore(
                    services.settings.data_root
                    / 'automation/automation.sqlite3'
                ),
                profiles_path=profiles_path,
                gerrit_query=query_gerrit,
                device_selector=device_selector,
                device_manager=device_manager,
                cluster_provider=get_cluster_service,
            )
        )
        build_config_path = build_servers_path(services.settings.project_root)
        configure_build_service(
            BuildService(
                store=BuildStore(
                    services.settings.data_root / 'build/build.sqlite3'
                ),
                config_path=build_config_path,
            )
        )
    init_templates(templates)
    for router in ALL_ROUTERS:
        app.include_router(router)
