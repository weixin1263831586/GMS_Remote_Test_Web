from __future__ import annotations

from typing import Any


config_manager: Any = None
ssh_manager: Any = None
global_state: Any = None
project_root: Any = None
safe_websocket_send: Any = None
generate_help_or_continue: Any = None
get_client_id_from_request: Any = None
parse_client_id: Any = None
store_notification: Any = None
apk_max_file_size: int = 0
apk_upload_dir: Any = None
max_log_entries: int = 10000
create_apk_task: Any = None
normalize_apk_filename: Any = None
safe_join: Any = None
cleanup_files: Any = None
acquire_test_devices: Any = None
release_test_devices: Any = None
start_cluster_test: Any = None


def configure_runtime(
    *,
    selected_config_manager: Any,
    selected_ssh_manager: Any,
    selected_global_state: Any,
    selected_project_root: Any,
    selected_safe_websocket_send: Any = None,
    selected_generate_help_or_continue: Any = None,
    selected_get_client_id_from_request: Any = None,
    selected_parse_client_id: Any = None,
    selected_store_notification: Any = None,
    selected_apk_max_file_size: int = 0,
    selected_apk_upload_dir: Any = None,
    selected_max_log_entries: int = 10000,
    selected_create_apk_task: Any = None,
    selected_normalize_apk_filename: Any = None,
    selected_safe_join: Any = None,
    selected_cleanup_files: Any = None,
    selected_acquire_test_devices: Any = None,
    selected_release_test_devices: Any = None,
    selected_start_cluster_test: Any = None,
) -> None:
    global config_manager, ssh_manager, global_state, project_root
    global safe_websocket_send, generate_help_or_continue
    global get_client_id_from_request, parse_client_id, store_notification
    global apk_max_file_size, apk_upload_dir, max_log_entries
    global create_apk_task, normalize_apk_filename, safe_join, cleanup_files
    global acquire_test_devices, release_test_devices, start_cluster_test
    config_manager = selected_config_manager
    ssh_manager = selected_ssh_manager
    global_state = selected_global_state
    project_root = selected_project_root
    safe_websocket_send = selected_safe_websocket_send
    generate_help_or_continue = selected_generate_help_or_continue
    get_client_id_from_request = selected_get_client_id_from_request
    parse_client_id = selected_parse_client_id
    store_notification = selected_store_notification
    apk_max_file_size = selected_apk_max_file_size
    apk_upload_dir = selected_apk_upload_dir
    max_log_entries = selected_max_log_entries
    create_apk_task = selected_create_apk_task
    normalize_apk_filename = selected_normalize_apk_filename
    safe_join = selected_safe_join
    cleanup_files = selected_cleanup_files
    acquire_test_devices = selected_acquire_test_devices
    release_test_devices = selected_release_test_devices
    start_cluster_test = selected_start_cluster_test
