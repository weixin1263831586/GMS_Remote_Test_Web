from __future__ import annotations

from fastapi import FastAPI

from core.api_help import generate_help_or_continue
from core.clients import (
    get_client_id_from_request,
    get_client_ip,
    probe_windows_usbipd,
    resolve_tailscale_device_host,
)
from core.config import config_manager
from core.file_utils import FileUtils
from core.notifications import safe_websocket_send, store_notification
from core.settings import DEVICE_CACHE_TTL, PROJECT_ROOT
from core.ssh import ssh_manager
from core.state import global_state
from core.universal_ai import get_universal_analyzer
from features.automation.api import configure_automation_service
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from features.devices.dependencies import configure_device_dependencies
from features.devices.network import run_local_shell_command
from features.gerrit.dependencies import configure_redmine_users_provider
from features.gerrit.service import _query_gerrit_dual_mode
from features.redmine.api import configure_redmine_service
from features.reports.dependencies import configure_report_dependencies
from modules.client_manager import client_manager
from routers import ALL_ROUTERS
from routers.system import init_templates
from routers.tests import (
    _make_empty_suite_target,
    _resolve_suite_diagnosis_target,
)


def include_routes(app: FastAPI, templates, services=None) -> None:
    if services is not None:
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
        configure_report_dependencies(
            ssh_manager=ssh_manager,
            file_utils=FileUtils,
            universal_analyzer_factory=get_universal_analyzer,
            resolve_suite_target=_resolve_suite_diagnosis_target,
            make_empty_suite_target=_make_empty_suite_target,
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
