#!/usr/bin/env python3
"""Safely extract a trusted deployment ZIP while restoring Unix modes."""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path


def extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            target = (root / item.filename).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"unsafe ZIP member: {item.filename}")
            bundle.extract(item, root)
            mode = (item.external_attr >> 16) & 0o7777
            if mode and target.exists():
                os.chmod(target, mode)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: extract_zip_preserve_mode.py ARCHIVE DESTINATION")
    extract_archive(Path(sys.argv[1]), Path(sys.argv[2]))
