from __future__ import annotations

import hashlib
import re
from pathlib import Path


def owner_storage_key(owner_id: str) -> str:
    """Return a stable, filesystem-safe owner directory name."""
    raw = str(owner_id or "anonymous").strip() or "anonymous"
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "anonymous"
    if readable != raw or len(readable) > 160:
        suffix = f"_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        return readable[:160 - len(suffix)] + suffix
    return readable


def owner_preferences_dir(data_root: Path | str, owner_id: str) -> Path:
    """Return the canonical owner preferences directory."""
    directory = Path(data_root) / 'user_prefs' / owner_storage_key(owner_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
