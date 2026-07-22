"""Typed test-execution runtime bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


RuntimeCallable = Callable[..., object]


@dataclass
class TestExecutionRuntime:
    config_manager: object | None = None
    ssh_manager: object | None = None
    global_state: object | None = None
    project_root: Path | None = None
    safe_websocket_send: RuntimeCallable | None = None
    generate_help_or_continue: RuntimeCallable | None = None
    get_client_id_from_request: RuntimeCallable | None = None
    apk_max_file_size: int = 0
    apk_upload_dir: Path | None = None
    max_log_entries: int = 10000
    create_apk_task: RuntimeCallable | None = None
    normalize_apk_filename: RuntimeCallable | None = None
    safe_join: RuntimeCallable | None = None
    cleanup_files: RuntimeCallable | None = None
    start_cluster_test: RuntimeCallable | None = None
    suite_task_store: object | None = None


_runtime = TestExecutionRuntime()
_RUNTIME_FIELDS = frozenset(TestExecutionRuntime.__dataclass_fields__)


def get_runtime() -> TestExecutionRuntime:
    return _runtime


def __getattr__(name: str) -> object:
    if name in _RUNTIME_FIELDS:
        return getattr(_runtime, name)
    raise AttributeError(name)


def configure_runtime(
    *,
    selected_config_manager: object,
    selected_ssh_manager: object,
    selected_global_state: object,
    selected_project_root: Path,
    selected_safe_websocket_send: RuntimeCallable | None = None,
    selected_generate_help_or_continue: RuntimeCallable | None = None,
    selected_get_client_id_from_request: RuntimeCallable | None = None,
    selected_apk_max_file_size: int = 0,
    selected_apk_upload_dir: Path | None = None,
    selected_max_log_entries: int = 10000,
    selected_create_apk_task: RuntimeCallable | None = None,
    selected_normalize_apk_filename: RuntimeCallable | None = None,
    selected_safe_join: RuntimeCallable | None = None,
    selected_cleanup_files: RuntimeCallable | None = None,
    selected_start_cluster_test: RuntimeCallable | None = None,
    selected_suite_task_store: object | None = None,
) -> None:
    for name in _RUNTIME_FIELDS:
        globals().pop(name, None)
    _runtime.config_manager = selected_config_manager
    _runtime.ssh_manager = selected_ssh_manager
    _runtime.global_state = selected_global_state
    _runtime.project_root = Path(selected_project_root)
    _runtime.safe_websocket_send = selected_safe_websocket_send
    _runtime.generate_help_or_continue = selected_generate_help_or_continue
    _runtime.get_client_id_from_request = selected_get_client_id_from_request
    _runtime.apk_max_file_size = int(selected_apk_max_file_size)
    _runtime.apk_upload_dir = (
        Path(selected_apk_upload_dir) if selected_apk_upload_dir else None
    )
    _runtime.max_log_entries = int(selected_max_log_entries)
    _runtime.create_apk_task = selected_create_apk_task
    _runtime.normalize_apk_filename = selected_normalize_apk_filename
    _runtime.safe_join = selected_safe_join
    _runtime.cleanup_files = selected_cleanup_files
    _runtime.start_cluster_test = selected_start_cluster_test
    _runtime.suite_task_store = selected_suite_task_store
