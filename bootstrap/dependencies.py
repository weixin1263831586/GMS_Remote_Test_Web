from __future__ import annotations

from dataclasses import dataclass

from features.assistant.universal_ai import UniversalAIAnalyzer
from features.redmine.agent import RedmineAgent
from features.redmine.repository import RedmineAgentDB
from features.redmine.service import RedmineService
from features.reports import ReportAnalyzer
from foundation.config import ConfigManager, RuntimeSettings, settings


@dataclass
class AppServices:
    config: ConfigManager
    settings: RuntimeSettings
    redmine: RedmineService


def build_services(
    *,
    runtime_settings: RuntimeSettings | None = None,
) -> AppServices:
    selected = settings if runtime_settings is None else runtime_settings
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
