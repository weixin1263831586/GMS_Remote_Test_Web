"""Resolve required Worker-native executors from installed or source layouts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
from pathlib import Path, PurePosixPath


class NativeToolUnavailableError(RuntimeError):
    pass


def _command_available(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    executable = argv[0]
    if os.path.sep in executable:
        return os.path.isfile(executable) and os.access(executable, os.X_OK)
    return shutil.which(executable) is not None


def resolve_native_tool(env_name: str, binary_name: str) -> str:
    """Return an executable command or fail; there is no Python fallback."""
    override = os.getenv(env_name, "").strip()
    if override:
        if _command_available(override):
            return override
        raise NativeToolUnavailableError(
            f"native tool configured by {env_name} is not executable"
        )

    project_or_install_root = Path(__file__).resolve().parent.parent
    candidates = (
        project_or_install_root / "bin" / binary_name,
        project_or_install_root / "tools" / "gms-worker-native" / "dist"
        / platform.machine() / binary_name,
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    discovered = shutil.which(binary_name)
    if discovered:
        return discovered
    raise NativeToolUnavailableError(
        f"required native Worker tool is unavailable: {binary_name}"
    )


COMPILED_WORKER_MANIFEST = "compiled-worker-files.json"
_COMPILED_PRIVATE_ROOTS = {"configs", "data", "docs", "web", "tools", "scripts", "agent", "plugins"}
_COMPILED_FORBIDDEN_SUFFIXES = {".py", ".pyc", ".pyo", ".pyi", ".pyx", ".map", ".c", ".cpp"}


def _compiled_member(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not relative.parts or relative.is_absolute() or relative.as_posix() != name
        or ".." in relative.parts or relative.parts[0] in _COMPILED_PRIVATE_ROOTS
        or any(part.startswith(".") or part in {"tests", "test", "__pycache__"} for part in relative.parts)
        or relative.suffix.lower() in _COMPILED_FORBIDDEN_SUFFIXES
        or name == COMPILED_WORKER_MANIFEST
    ):
        raise RuntimeError(f"unsafe compiled Worker member: {name}")
    path = root / relative
    if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
        raise RuntimeError(f"compiled Worker member missing or not a regular file: {name}")
    return path


def _compiled_file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_compiled_worker_manifest(root: Path) -> None:
    """Record only the native standalone build, before Controller assets are staged."""
    root = root.resolve()
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        name = path.relative_to(root).as_posix()
        files[name] = _compiled_file_digest(_compiled_member(root, name))
    if "gms-runtime" not in files or not os.access(root / "gms-runtime", os.X_OK):
        raise RuntimeError("compiled Worker executable missing")
    (root / COMPILED_WORKER_MANIFEST).write_text(
        json.dumps(files, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )


def compiled_worker_members(root: Path) -> list[tuple[str, Path]]:
    """Resolve only signed-build members, verifying content before deployment."""
    root = root.resolve()
    manifest = root / COMPILED_WORKER_MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError("compiled Worker manifest missing")
    files = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(files, dict) or "gms-runtime" not in files:
        raise RuntimeError("invalid compiled Worker manifest")
    result = []
    for name, expected in sorted(files.items()):
        path = _compiled_member(root, name)
        if _compiled_file_digest(path) != expected:
            raise RuntimeError(f"compiled Worker checksum mismatch: {name}")
        result.append((name, path))
    if not os.access(root / "gms-runtime", os.X_OK):
        raise RuntimeError("compiled Worker executable is not executable")
    return result
