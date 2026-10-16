"""Initialize each local SQLite file once per process without serializing reads."""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path


_lock = threading.Lock()
_initialized: OrderedDict[Path, tuple[int, ...]] = OrderedDict()
_MAX_DATABASES = 256


def _identity(conn, path: Path) -> tuple[int, ...]:
    info = path.stat()
    # Schema cookies change on DDL, but ordinary writes leave them alone.
    # A truncated/rebuilt file can keep its inode, so inode alone is insufficient.
    return (
        os.getpid(), info.st_dev, info.st_ino,
        conn.execute('PRAGMA schema_version').fetchone()[0],
        conn.execute('PRAGMA user_version').fetchone()[0],
    )


def invalidate_schema(path: Path) -> None:
    """Forget initialization after an explicit database restore.

    The shipped restore flow (scripts/gms_backup.py) is offline by contract:
    it takes the controller process lock and refuses to run while the Web
    process, Worker or CLI is alive, so a live process never sees its SQLite
    files replaced mid-flight.  This hook exists for any future in-process
    restore path: call it right after the file is replaced so the next
    connect() re-runs the initializer instead of trusting a stale identity.
    """
    with _lock:
        _initialized.pop(Path(path).resolve(), None)


def ensure_schema(conn, path: Path, initialize) -> None:
    """Check file/schema identity; initializers version migrations with user_version."""
    path = Path(path).resolve()
    identity = _identity(conn, path)
    with _lock:
        if _initialized.get(path) == identity:
            _initialized.move_to_end(path)
            return
        # Another reader may have initialized this file while we waited.
        identity = _identity(conn, path)
        if _initialized.get(path) == identity:
            _initialized.move_to_end(path)
            return
        try:
            initialize(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            _initialized.pop(path, None)
            raise
        _initialized[path] = _identity(conn, path)
        _initialized.move_to_end(path)
        while len(_initialized) > _MAX_DATABASES:
            _initialized.popitem(last=False)
