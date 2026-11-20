"""Cross-process, crash-released lock for explicitly requested background syncs.

The file descriptor stays open for the full job lifetime. An exited process
releases the kernel lock automatically, unlike persistent boolean flags.
"""
from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


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
