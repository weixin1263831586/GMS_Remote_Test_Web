from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .runtime import configure_runtime


def configure_test_execution_dependencies(
    *,
    config_manager: object,
    ssh_manager: object,
    global_state: object,
    project_root: Path,
    safe_websocket_send: Callable[..., object],
    generate_help_or_continue: Callable[..., object],
    get_client_id_from_request: Callable[..., object],
    apk_max_file_size: int,
    apk_upload_dir: Path,
    max_log_entries: int,
    create_apk_task: Callable[..., object],
    normalize_apk_filename: Callable[..., object],
    safe_join: Callable[..., object],
    cleanup_files: Callable[..., object],
    start_cluster_test: Callable[..., object] | None = None,
    suite_task_store: object | None = None,
) -> None:
    configure_runtime(
        selected_config_manager=config_manager,
        selected_ssh_manager=ssh_manager,
        selected_global_state=global_state,
        selected_project_root=project_root,
        selected_safe_websocket_send=safe_websocket_send,
        selected_generate_help_or_continue=generate_help_or_continue,
        selected_get_client_id_from_request=get_client_id_from_request,
        selected_apk_max_file_size=apk_max_file_size,
        selected_apk_upload_dir=apk_upload_dir,
        selected_max_log_entries=max_log_entries,
        selected_create_apk_task=create_apk_task,
        selected_normalize_apk_filename=normalize_apk_filename,
        selected_safe_join=safe_join,
        selected_cleanup_files=cleanup_files,
        selected_start_cluster_test=start_cluster_test,
        selected_suite_task_store=suite_task_store,
    )
