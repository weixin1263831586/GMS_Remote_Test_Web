from __future__ import annotations

from fastapi import FastAPI

from core.config import config_manager
from core.file_utils import FileUtils
from core.ssh import ssh_manager
from core.universal_ai import get_universal_analyzer
from features.automation.api import configure_automation_service
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from features.gerrit.dependencies import configure_redmine_users_provider
from features.gerrit.service import _query_gerrit_dual_mode
from features.redmine.api import configure_redmine_service
from features.reports.dependencies import configure_report_dependencies
from routers import ALL_ROUTERS
from routers.system import init_templates
from routers.tests import (
    _make_empty_suite_target,
    _resolve_suite_diagnosis_target,
)


def include_routes(app: FastAPI, templates, services=None) -> None:
    if services is not None:
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
