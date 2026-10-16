"""Cache validated keys only while the source remains unchanged during loading."""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from threading import Lock


def key_file_fingerprint(path: Path) -> tuple[int, ...] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (
        info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns,
        info.st_size, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
    )


def cached_key(
    cache: dict,
    lock: Lock,
    cache_key: str,
    fingerprint: Callable[[], object],
    load: Callable[[], bytes],
) -> bytes:
    # Serialize misses as well as publication: a slow reader must not overwrite
    # a newer key, and concurrent cold readers should validate the file once.
    with lock:
        for _ in range(3):
            before = fingerprint()
            cached = cache.get(cache_key)
            if before is not None and cached is not None and cached[0] == before:
                return cached[1]
            cache.pop(cache_key, None)
            key = load()
            after = fingerprint()
            if before is not None and before == after:
                cache[cache_key] = (after, key)
                return key
        raise RuntimeError("Key source changed repeatedly while loading; retry the operation")
