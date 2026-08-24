from __future__ import annotations

from dataclasses import dataclass

from features.assistant.universal_ai import UniversalAIAnalyzer
from features.automation import register_worker_status_port
from features.cluster import register_cluster_port
from features.devices.locks import devices_display_name_resolver
from features.email import configure_manager_provider
from features.redmine import (
    configure_agent_factories,
    get_redmine_config_for_request,
)
from features.redmine.agent import RedmineAgent
from features.redmine.repository import RedmineAgentDB
from features.redmine.service import RedmineService
from features.reports import ReportAnalyzer
from foundation.config import ConfigManager, RuntimeSettings, settings
from foundation.device_locks import configure_display_name_resolver


@dataclass
class AppServices:
    config: ConfigManager
    settings: RuntimeSettings
    redmine: RedmineService


def _wire_cross_feature_services() -> None:
    """Connect cross-feature seams that features cannot wire themselves.

    - cluster / automation: register the foundation access ports that
      devices / reports / system / test_execution / users consume.
    - email: per-request owner config manager (SMTP credentials live in the
      redmine config tree).
    - redmine: per-owner agent analyzer factories (reports / assistant).
    - device locks: identity-aware owner display-name resolver.
    """
    register_cluster_port()
    register_worker_status_port()
    configure_manager_provider(get_redmine_config_for_request)
    configure_agent_factories(
        report_analyzer_factory=ReportAnalyzer,
        ai_analyzer_factory=UniversalAIAnalyzer,
    )
    configure_display_name_resolver(devices_display_name_resolver)


def build_services(
    *,
    runtime_settings: RuntimeSettings | None = None,
) -> AppServices:
    selected = settings if runtime_settings is None else runtime_settings
    _wire_cross_feature_services()
    redmine_repository = RedmineAgentDB(
        db_path=selected.data_root / "redmine/redmine.sqlite3",
        docs_dir=selected.data_root / "redmine/docs",
    )
    return AppServices(
        config=ConfigManager(project_root=selected.project_root),
        settings=selected,
        redmine=RedmineService(
            repository=redmine_repository,
            agent=RedmineAgent(
                redmine_repository,
                report_analyzer_factory=ReportAnalyzer,
                ai_analyzer_factory=UniversalAIAnalyzer,
            ),
        ),
    )
