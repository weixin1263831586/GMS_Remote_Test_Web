"""Per-user sidebar order and visibility persistence."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import runtime
from .storage_paths import owner_storage_key


_storage_lock = threading.RLock()


def _preferences_path(owner_id: str) -> Path:
    directory = Path(runtime.data_root) / "user_prefs" / owner_storage_key(owner_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "navigation.json"


def load_navigation_preferences(owner_id: str) -> dict[str, list[str]]:
    path = _preferences_path(owner_id)
    with _storage_lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "order": data.get("order") if isinstance(data.get("order"), list) else [],
        "visible_pages": (
            data.get("visible_pages")
            if isinstance(data.get("visible_pages"), list)
            else []
        ),
    }


def save_navigation_preferences(
    owner_id: str,
    updates: dict[str, Any],
) -> dict[str, list[str]]:
    path = _preferences_path(owner_id)
    with _storage_lock:
        current = load_navigation_preferences(owner_id)
        for key in ("order", "visible_pages"):
            if key in updates:
                current[key] = list(updates[key])
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="navigation-",
            suffix=".json",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return current
