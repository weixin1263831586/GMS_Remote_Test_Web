"""Locate the bundled tesseract OCR engine shipped with the project.

The project bundles tesseract binaries + shared libraries + chi_sim/eng traineddata
so that image OCR works in environments where apt/sudo is unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path:
    # The project places feature packages under web_app/features; walk up from
    # features/redmine/tesseract_finder.py to reach the repository root.
    return Path(__file__).resolve().parents[2]


def _tesseract_dir() -> Path:
    return _project_root() / "tools" / "tesseract"


def bundled_tesseract_cmd() -> str | None:
    """Return the path to the bundled tesseract binary, or None if missing."""
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


def configure_bundled_tesseract() -> bool:
    """If a bundled tesseract exists, update process env so subprocess can find it.

    Returns True when the bundled binary was configured.
    """
    if bundled_tesseract_cmd() is None:
        return False
    for key, value in bundled_tesseract_env().items():
        os.environ[key] = value
    return True
