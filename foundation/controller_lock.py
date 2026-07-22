"""Single-controller process lock for the SQLite deployment profile."""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


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
