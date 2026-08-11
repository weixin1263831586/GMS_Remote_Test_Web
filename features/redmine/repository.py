"""Persistence facade for the Redmine feature."""

from .repository_queries import RepositoryQueryMixin
from .repository_schema import RepositorySchemaMixin
from .repository_storage import RepositoryStorageMixin
from .users import (
    DB_PATH,
    DOCS_DIR,
    RESOLVED_STATUS_NAMES,
    _looks_like_rk_actor,
    _parse_dt,
    _sorted_slice,
    _time_key,
    compute_user_overdue_stats,
    display_names_from_mapping,
    find_user_mapping,
    find_user_mapping_for_names,
    load_redmine_user_map_for_owner,
    load_user_map_payload_for_owner,
    name_keys,
    norm_name,
    owner_attachments_dir,
    owner_db_path,
    owner_docs_dir,
    owner_knowledge_db_path,
    owner_runtime_config_path,
    owner_user_map_path,
    refresh_assignee_issue_snapshots,
    save_user_map_payload_for_owner,
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
    "RedmineAgentDB",
    "_looks_like_rk_actor",
    "_parse_dt",
    "_sorted_slice",
    "_time_key",
    "compute_user_overdue_stats",
    "display_names_from_mapping",
    "find_user_mapping",
    "find_user_mapping_for_names",
    "load_redmine_user_map_for_owner",
    "load_user_map_payload_for_owner",
    "name_keys",
    "norm_name",
    "owner_attachments_dir",
    "owner_db_path",
    "owner_docs_dir",
    "owner_knowledge_db_path",
    "owner_runtime_config_path",
    "owner_user_map_path",
    "refresh_assignee_issue_snapshots",
    "save_user_map_payload_for_owner",
]
