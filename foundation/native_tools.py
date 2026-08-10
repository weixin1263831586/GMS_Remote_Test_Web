"""Resolve required Worker-native executors from installed or source layouts."""

from __future__ import annotations

import os
import platform
import shlex
import shutil
from pathlib import Path


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
