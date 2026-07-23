from __future__ import annotations

import hashlib
import re


def owner_storage_key(owner_id: str) -> str:
    """Return a stable, filesystem-safe owner directory name."""
    raw = str(owner_id or "anonymous").strip() or "anonymous"
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "anonymous"
    if readable != raw:
        readable = f"{readable}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
    return readable[:160]
