"""Typed and validated APK-feature runtime bindings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from foundation.runtime_contracts import ClientIdResolver


@dataclass
class ApkRuntime:
    config_manager: object | None = None
    global_state: object | None = None
    get_client_id_from_request: ClientIdResolver | None = None
    project_root: Path | None = None
    apk_upload_dir: Path | None = None
    apk_max_tasks: int = 20
    apk_max_file_size: int = 0
    apk_max_source_file_size: int = 0
    jadx_path: str = "jadx"
    jadx_timeout: int = 300
    apk_task_store: object | None = None


_runtime = ApkRuntime()
_RUNTIME_FIELDS = frozenset(ApkRuntime.__dataclass_fields__)


def get_runtime() -> ApkRuntime:
    return _runtime


def __getattr__(name: str) -> object:
    if name in _RUNTIME_FIELDS:
        return getattr(_runtime, name)
    raise AttributeError(name)


def configure_runtime(**values: object) -> None:
    invalid = set(values) - _RUNTIME_FIELDS
    if invalid:
        raise TypeError(f"unknown APK runtime bindings: {sorted(invalid)}")
    for name in _RUNTIME_FIELDS:
        globals().pop(name, None)
    for name, value in values.items():
        if name in {"project_root", "apk_upload_dir"} and value is not None:
            value = Path(value)
        setattr(_runtime, name, value)

    if values.get("apk_upload_dir"):
        from .store import ApkTaskStore

        _runtime.apk_task_store = ApkTaskStore(
            Path(values["apk_upload_dir"]) / "tasks.sqlite3"
        )
    state = values.get("global_state")
    store = _runtime.apk_task_store
    if state is not None and store is not None:
        with state.apk_analysis_tasks_lock:
            state.apk_analysis_tasks.clear()
            state.apk_analysis_tasks.update(store.list())
