from typing import Any

from fastapi import Path, Query, Request
from fastapi.responses import JSONResponse

from .agent import RESOLVED_STATUSES
from .api import (
    configure_agent_factories,
    get_redmine_config_for_request,
    get_redmine_service_for_owner,
    get_redmine_service_for_request,
    redmine_service,
    resolve_owner_names,
)
from .cases.case_extractor import RedmineCaseExtractor
from .client import RedmineClient
from .config import config_manager
from .failure_identity import failure_identity
from .indexing import redmine_index_jobs
from .org_chart import load_redmine_user_map_for_owner
from .repository import (
    display_names_from_mapping,
    find_user_mapping,
    name_keys,
    norm_name,
)
from .service import RedmineService
from .utils import (
    COMPILED_REDMINE_ATTACHMENT_PATTERN,
    COMPILED_REDMINE_ISSUE_PATTERN,
    COMPILED_REPORT_NAME_PATTERN,
    REDMINE_ISSUE_PATTERN,
    bounded_stale_days,
    create_basic_auth_header,
    extract_filename_from_content_disposition,
    extract_redmine_issue_id_from_text,
    strip_redmine_report_prefix,
)


async def get_workload_statistics(
    request: Request,
    stale_days: int | None = Query(None, ge=1, le=30),
    list_limit: int = Query(30, ge=1, le=100),
    name: str = Query(""),
    refresh: bool = Query(False),
) -> dict[str, Any] | JSONResponse:
    from .statistics_api import get_workload_statistics as implementation

    return await implementation(request, stale_days=stale_days, list_limit=list_limit, name=name, refresh=refresh)


async def get_department_overdue_statistics(
    request: Request,
    *,
    stale_days: int | None = None,
    list_limit: int | None = None,
    issue_limit: int | None = None,
    profile_id: str = "",
    refresh: bool = False,
) -> dict[str, Any] | JSONResponse:
    from .statistics_api import get_department_overdue_statistics as implementation

    return await implementation(
        request, stale_days=stale_days, list_limit=list_limit, issue_limit=issue_limit,
        profile_id=profile_id, refresh=refresh,
    )


async def analyze_issue_for_assistant(
    request: Request, issue_ref: str, analysis_mode: str = "incremental", analysis_hint: str = "",
) -> dict[str, Any] | JSONResponse:
    from .daily_brief.api import analyze_issue_for_assistant as implementation

    return await implementation(request, issue_ref, analysis_mode, analysis_hint)


async def get_analysis_for_assistant(request: Request, run_id: str) -> dict[str, Any] | JSONResponse:
    from .daily_brief.api import get_analysis_for_assistant as implementation

    return await implementation(request, run_id)


async def get_analysis_events_for_assistant(
    request: Request,
    run_id: str,
    issue_id: int = Path(ge=1),
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any] | JSONResponse:
    """Public Redmine facade for owner-checked analysis progress events."""
    from .daily_brief.api import get_issue_analysis_events as implementation

    return await implementation(request, run_id, issue_id, after_sequence=after_sequence, limit=limit)


__all__ = [
    "COMPILED_REDMINE_ATTACHMENT_PATTERN",
    "COMPILED_REDMINE_ISSUE_PATTERN",
    "COMPILED_REPORT_NAME_PATTERN",
    "REDMINE_ISSUE_PATTERN",
    "RESOLVED_STATUSES",
    "RedmineCaseExtractor",
    "RedmineClient",
    "RedmineService",
    "analyze_issue_for_assistant",
    "bounded_stale_days",
    "config_manager",
    "configure_agent_factories",
    "create_basic_auth_header",
    "display_names_from_mapping",
    "extract_filename_from_content_disposition",
    "extract_redmine_issue_id_from_text",
    "failure_identity",
    "find_user_mapping",
    "get_analysis_events_for_assistant",
    "get_analysis_for_assistant",
    "get_department_overdue_statistics",
    "get_redmine_config_for_request",
    "get_redmine_service_for_owner",
    "get_redmine_service_for_request",
    "get_workload_statistics",
    "load_redmine_user_map_for_owner",
    "name_keys",
    "norm_name",
    "redmine_index_jobs",
    "redmine_service",
    "resolve_owner_names",
    "strip_redmine_report_prefix",
]
