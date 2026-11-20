#!/usr/bin/env python3
"""Fail-closed scanner for compiled, independent distribution trees.

The user-supplied release must never contain project Python source, bytecode,
development secrets, repository metadata, or nested archives containing them.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_SUFFIXES = {".py", ".pyc", ".pyo", ".pyi", ".pyx", ".pyd.map", ".map", ".c", ".cpp"}
FORBIDDEN_PARTS = {".git", ".github", ".venv", "__pycache__", ".pytest_cache", "tests", "test"}
FORBIDDEN_MARKERS = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
)
MAX_TEXT_SCAN = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100000


def _forbidden(path: str) -> bool:
    posix = PurePosixPath(path.replace("\\", "/"))
    name = posix.name.lower()
    parts = {part.lower() for part in posix.parts}
    if not name or name in {".", ".."}:
        return False
    if parts & FORBIDDEN_PARTS:
        return True
    if name == "agents.md" or name.startswith(".env") or name.endswith(".map"):
        return True
    if path.endswith(("/configs/local", "/configs/secrets")):
        return True
    if {"configs", "local"}.issubset(parts) or {"configs", "secrets"}.issubset(parts):
        return True
    return any(name.endswith(ext) for ext in FORBIDDEN_SUFFIXES)


def inspect(root: Path) -> list[str]:
    root = root.resolve()
    issues = []
    if not root.is_dir():
        return [f"release tree missing: {root}"]
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            # Local Nuitka-owned symlinks are okay only if they stay inside the bundle.
            if not path.resolve().is_relative_to(root):
                issues.append(f"escaping symlink: {rel}")
            continue
        if _forbidden(rel):
            issues.append(f"source or private path: {rel}")
        if not path.is_file():
            continue
        if path.stat().st_size <= MAX_TEXT_SCAN and path.suffix.lower() in {".txt", ".json", ".toml", ".yaml", ".yml", ".sh", ".html", ".pem"}:
            blob = path.read_bytes()
            if any(marker in blob for marker in FORBIDDEN_MARKERS):
                issues.append(f"private-key material: {rel}")
        try:
            lower_name = path.name.lower()
            if lower_name.endswith((".zip", ".whl", ".egg", ".jar")) and zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    if len(zf.infolist()) > MAX_ARCHIVE_MEMBERS:
                        issues.append(f"archive too many entries: {rel}")
                    for member in zf.infolist()[:MAX_ARCHIVE_MEMBERS]:
                        if _forbidden(member.filename):
                            issues.append(f"nested source: {rel}!{member.filename}")
            elif lower_name.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")) and tarfile.is_tarfile(path):
                with tarfile.open(path, "r:*") as archive:
                    for idx, member in enumerate(archive):
                        if idx > MAX_ARCHIVE_MEMBERS:
                            issues.append(f"archive too many entries: {rel}")
                            break
                        if _forbidden(member.name):
                            issues.append(f"nested source: {rel}!{member.name}")
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
            issues.append(f"uninspectable nested archive: {rel}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    issues = inspect(args.directory)
    print(json.dumps({"ok": not issues, "issues": issues}, ensure_ascii=False, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
