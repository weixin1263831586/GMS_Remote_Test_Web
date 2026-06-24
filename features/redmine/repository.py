"""Persistence facade for the Redmine feature."""

from .repository_queries import RepositoryQueryMixin
from .repository_schema import RepositorySchemaMixin
from .repository_storage import RepositoryStorageMixin
from .users import (
    DB_PATH,
    DOCS_DIR,
    RESOLVED_STATUS_NAMES,
    USER_MAP_PATH,
    _looks_like_rk_actor,
    _name_keys,
    _norm_name,
    _parse_dt,
    _sorted_slice,
    _time_key,
    compute_user_overdue_stats,
    display_names_from_mapping,
    find_user_mapping,
    load_redmine_user_map,
    load_user_map_payload,
    save_user_map_payload,
)


class RedmineAgentDB(
    RepositorySchemaMixin,
    RepositoryQueryMixin,
    RepositoryStorageMixin,
):
    """SQLite repository for Redmine feature data."""


__all__ = [
    "DB_PATH",
    "DOCS_DIR",
    "RESOLVED_STATUS_NAMES",
    "USER_MAP_PATH",
    "RedmineAgentDB",
    "_looks_like_rk_actor",
    "_name_keys",
    "_norm_name",
    "_parse_dt",
    "_sorted_slice",
    "_time_key",
    "compute_user_overdue_stats",
    "display_names_from_mapping",
    "find_user_mapping",
    "load_redmine_user_map",
    "load_user_map_payload",
    "save_user_map_payload",
]
