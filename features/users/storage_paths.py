from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def owner_storage_key(owner_id: str) -> str:
    """Return a stable, filesystem-safe owner directory name."""
    raw = str(owner_id or "anonymous").strip() or "anonymous"
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "anonymous"
    if readable != raw:
        readable = f"{readable}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
    return readable[:160]


def atomic_write_json(path: Path, payload: Any) -> None:
    """mkstemp + fsync + os.replace 原子写 JSON；失败时清理临时文件。"""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}-", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
