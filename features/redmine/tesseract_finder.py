"""Locate the bundled tesseract OCR engine shipped with the project.

The project bundles tesseract binaries + shared libraries + chi_sim/eng traineddata
so that image OCR works in environments where apt/sudo is unavailable.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path


def _project_root() -> Path:
    # 从模块路径定位项目根目录，不依赖进程工作目录。
    return Path(__file__).resolve().parents[2]


def _tesseract_dir() -> Path:
    return _project_root() / "tools" / "tesseract"


@functools.lru_cache(maxsize=1)
def bundled_tesseract_cmd() -> str | None:
    """Return the path to the bundled tesseract binary, or None if missing.

    Cached: the binary location is fixed for the process lifetime, so the
    ``.exists()`` probe runs only once; later calls reuse the result.
    """
    cmd = _tesseract_dir() / "tesseract"
    return str(cmd) if cmd.exists() else None


def bundled_tesseract_env() -> dict[str, str]:
    """Return env vars needed for the bundled tesseract to run.

    Adds the bundled lib directory to LD_LIBRARY_PATH and sets TESSDATA_PREFIX.
    """
    root = _tesseract_dir()
    env = dict(os.environ)
    lib_path = str(root)
    env["LD_LIBRARY_PATH"] = lib_path + (os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env["TESSDATA_PREFIX"] = str(root / "tessdata")
    env["PATH"] = lib_path + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    return env


# Tesseract 路径和环境在进程内只探测一次。
_configured: bool | None = None


def configure_bundled_tesseract() -> bool:
    """If a bundled tesseract exists, update process env so subprocess can find it.

    Idempotent and memoized: the binary path and env are fixed, so this probes
    the filesystem and mutates ``os.environ`` only once per process. Subsequent
    calls return the cached result.
    """
    global _configured
    if _configured is not None:
        return _configured
    if bundled_tesseract_cmd() is None:
        _configured = False
        return False
    for key, value in bundled_tesseract_env().items():
        os.environ[key] = value
    _configured = True
    return True
