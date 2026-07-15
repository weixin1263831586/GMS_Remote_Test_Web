from .agent import RESOLVED_STATUSES
from .client import RedmineClient
from .api import (
    _resolve_owner_names,
    get_redmine_config_for_request,
    get_redmine_service_for_owner,
    get_redmine_service_for_request,
    redmine_service,
)
from .config import config_manager
from .repository import (
    _name_keys,
    _norm_name,
    display_names_from_mapping,
    find_user_mapping,
    load_redmine_user_map,
)
from .service import RedmineService
from .users import load_redmine_user_map_for_owner


async def get_workload_statistics(*args, **kwargs):
    from .statistics_api import get_workload_statistics as implementation

    return await implementation(*args, **kwargs)


__all__ = [
    "RESOLVED_STATUSES",
    "RedmineClient",
    "RedmineService",
    "_name_keys",
    "_norm_name",
    "_resolve_owner_names",
    "config_manager",
    "display_names_from_mapping",
    "find_user_mapping",
    "get_redmine_config_for_request",
    "get_redmine_service_for_owner",
    "get_redmine_service_for_request",
    "get_workload_statistics",
    "load_redmine_user_map",
    "load_redmine_user_map_for_owner",
    "redmine_service",
]
