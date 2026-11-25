"""SQLite connections and per-database schema initialization."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect_sqlite(path: Path | str, *, timeout: float = 30) -> Iterator[sqlite3.Connection]:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=timeout)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()


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
    """Invalidate cached schema identity after replacing a database file."""
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


def open_initialized_sqlite(path: Path, initialize, *, timeout: float = 10) -> sqlite3.Connection:
    """Open a caller-owned SQLite connection with row mapping and schema validation.

    Preserve the caller's transaction and journal settings; connect_sqlite is
    the separate managed WAL/foreign-key context for callers that need it.
    """
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    try:
        ensure_schema(connection, path, initialize)
    except BaseException:
        connection.close()
        raise
    return connection
