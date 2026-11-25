"""Cross-process, crash-released lock for explicitly requested background syncs.

The file descriptor stays open for the full job lifetime. An exited process
releases the kernel lock automatically, unlike persistent boolean flags.
"""
from __future__ import annotations

import errno
import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


def try_acquire_sync_lock(path: Path) -> int | None:
    """Acquire a nonblocking, process-wide exclusive lock; None means busy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None
        raise
    return descriptor


def release_sync_lock(descriptor: int) -> None:
    """Unlock and close a lock held throughout the worker's lifetime."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def sync_lock_held(path: Path) -> bool:
    """Return whether another live file descriptor holds this sync lock."""
    descriptor = try_acquire_sync_lock(path)
    if descriptor is None:
        return True
    release_sync_lock(descriptor)
    return False


@contextmanager
def hold_sync_lock(path: Path):
    """Serialize work that must wait for the previous owner to finish."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


_LOCK = threading.Lock()
_HANDLE: TextIO | None = None
_REFERENCES = 0


@contextmanager
def controller_process_lock(data_root: Path) -> Iterator[None]:
    """Reject a second Controller while allowing same-process test apps."""
    global _HANDLE, _REFERENCES
    with _LOCK:
        if _HANDLE is None:
            path = Path(data_root) / "controller.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+", encoding="utf-8")
            os.chmod(path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.seek(0)
                owner = handle.read().strip() or "unknown"
                handle.close()
                raise RuntimeError(
                    f"another GMS Controller already owns {path} (pid={owner})"
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
            _HANDLE = handle
        _REFERENCES += 1
    try:
        yield
    finally:
        with _LOCK:
            _REFERENCES -= 1
            if _REFERENCES == 0 and _HANDLE is not None:
                fcntl.flock(_HANDLE.fileno(), fcntl.LOCK_UN)
                _HANDLE.close()
                _HANDLE = None
