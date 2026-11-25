"""Private, atomic text and binary storage; no application initialization or secret logging."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, TextIO


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid configuration JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must be an object: {path.name}")
    return value


def _write_atomic(
    path: Path, writer: Callable[[TextIO | BinaryIO], object], *,
    sync_directory: bool = False, binary: bool = False,
) -> None:
    """Publish a complete private file through a uniquely created 0600 temporary file."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        try:
            handle = (os.fdopen(descriptor, "wb") if binary
                      else os.fdopen(descriptor, "w", encoding="utf-8"))
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        with handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if sync_directory:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def write_atomic_text(path: Path, payload: str, *, sync_directory: bool = False) -> None:
    _write_atomic(path, lambda handle: handle.write(payload), sync_directory=sync_directory)


def write_atomic_bytes(path: Path, payload: bytes, *, sync_directory: bool = False) -> None:
    """Publish binary evidence without decoding it or leaving partial files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, lambda handle: handle.write(payload),
                  sync_directory=sync_directory, binary=True)


def atomic_write_json(path: Path, value: Any) -> None:
    """Write user preference JSON, retaining the existing two-space format."""
    _write_atomic(path, lambda handle: json.dump(value, handle, ensure_ascii=False, indent=2))


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write(handle: TextIO) -> None:
        json.dump(value, handle, ensure_ascii=False, indent=4)
        handle.write("\n")

    _write_atomic(path, write, sync_directory=True)
