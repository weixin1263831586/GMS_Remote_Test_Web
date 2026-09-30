from .agent import RESOLVED_STATUSES
from .api import (
    configure_agent_factories,
    get_redmine_config_for_request,
    get_redmine_service_for_owner,
    get_redmine_service_for_request,
    redmine_service,
    resolve_owner_names,
)
from .case_extractor import RedmineCaseExtractor
from .client import RedmineClient
from .config import config_manager
from .failure_identity import failure_identity
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
    create_basic_auth_header,
    extract_filename_from_content_disposition,
    extract_redmine_issue_id_from_text,
    strip_redmine_report_prefix,
)


async def get_workload_statistics(*args, **kwargs):
    from .statistics_api import get_workload_statistics as implementation

    return await implementation(*args, **kwargs)


__all__ = [
    "COMPILED_REDMINE_ATTACHMENT_PATTERN",
    "COMPILED_REDMINE_ISSUE_PATTERN",
    "COMPILED_REPORT_NAME_PATTERN",
    "REDMINE_ISSUE_PATTERN",
    "RESOLVED_STATUSES",
    "RedmineCaseExtractor",
    "RedmineClient",
    "RedmineService",
    "config_manager",
    "configure_agent_factories",
    "create_basic_auth_header",
    "display_names_from_mapping",
    "extract_filename_from_content_disposition",
    "extract_redmine_issue_id_from_text",
    "failure_identity",
    "find_user_mapping",
    "get_redmine_config_for_request",
    "get_redmine_service_for_owner",
    "get_redmine_service_for_request",
    "get_workload_statistics",
    "load_redmine_user_map_for_owner",
    "name_keys",
    "norm_name",
    "redmine_service",
    "resolve_owner_names",
    "strip_redmine_report_prefix",
]
