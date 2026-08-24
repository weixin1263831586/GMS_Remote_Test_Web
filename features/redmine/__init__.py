from .agent import RESOLVED_STATUSES
from .api import (
    configure_agent_factories,
    get_redmine_config_for_request,
    get_redmine_service_for_owner,
    get_redmine_service_for_request,
    redmine_service,
    resolve_owner_names,
)
from .client import RedmineClient
from .config import config_manager
from .repository import (
    display_names_from_mapping,
    find_user_mapping,
    name_keys,
    norm_name,
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
    "config_manager",
    "configure_agent_factories",
    "display_names_from_mapping",
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
]
