"""Helpers for assembling the self-contained remote Worker bundle."""

from __future__ import annotations

import tarfile
from pathlib import Path


def add_worker_python_runtime(
    bundle: tarfile.TarFile,
    project_root: Path,
) -> None:
    """Bundle every in-repository Python package imported by the Worker."""
    bundle.add(project_root / "worker_agent", arcname="worker_agent")
    bundle.add(project_root / "foundation", arcname="foundation")
