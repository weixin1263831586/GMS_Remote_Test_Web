"""Per-user sidebar order and visibility persistence."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import runtime
from .storage_paths import atomic_write_json, owner_storage_key


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
        atomic_write_json(path, current)
    return current
