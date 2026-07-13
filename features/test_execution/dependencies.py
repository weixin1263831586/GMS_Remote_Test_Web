from __future__ import annotations

from typing import Any

from .runtime import configure_runtime


def configure_test_execution_dependencies(
    *,
    config_manager: Any,
    ssh_manager: Any,
    global_state: Any,
    project_root: Any,
    safe_websocket_send: Any,
    generate_help_or_continue: Any,
    get_client_id_from_request: Any,
    parse_client_id: Any,
    store_notification: Any,
    apk_max_file_size: int,
    apk_upload_dir: Any,
    max_log_entries: int,
    create_apk_task: Any,
    normalize_apk_filename: Any,
    safe_join: Any,
    cleanup_files: Any,
    acquire_test_devices: Any,
    release_test_devices: Any,
    start_cluster_test: Any = None,
) -> None:
    configure_runtime(
        selected_config_manager=config_manager,
        selected_ssh_manager=ssh_manager,
        selected_global_state=global_state,
        selected_project_root=project_root,
        selected_safe_websocket_send=safe_websocket_send,
        selected_generate_help_or_continue=generate_help_or_continue,
        selected_get_client_id_from_request=get_client_id_from_request,
        selected_parse_client_id=parse_client_id,
        selected_store_notification=store_notification,
        selected_apk_max_file_size=apk_max_file_size,
        selected_apk_upload_dir=apk_upload_dir,
        selected_max_log_entries=max_log_entries,
        selected_create_apk_task=create_apk_task,
        selected_normalize_apk_filename=normalize_apk_filename,
        selected_safe_join=safe_join,
        selected_cleanup_files=cleanup_files,
        selected_acquire_test_devices=acquire_test_devices,
        selected_release_test_devices=release_test_devices,
        selected_start_cluster_test=start_cluster_test,
    )
