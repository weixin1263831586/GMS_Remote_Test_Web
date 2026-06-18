from __future__ import annotations

from fastapi import FastAPI

from core.config import config_manager
from features.automation.api import configure_automation_service
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from features.redmine.api import configure_redmine_service
from routers import ALL_ROUTERS
from routers.gerrit_dashboard import _query_gerrit_dual_mode
from routers.system import init_templates


def include_routes(app: FastAPI, templates, services=None) -> None:
    if services is not None:
        configure_redmine_service(services.redmine)
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
