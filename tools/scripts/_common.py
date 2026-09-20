"""Shared helpers for tools/scripts/* maintenance utilities.

Every script under ``tools/scripts/`` needs the repository root, but the
scripts sit at different depths than the old flat ``tools/*.py`` layout and
hard-coding ``parents[3]`` everywhere would just move the brittleness.
Import this module from a sibling script with:

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _common import find_repo_root

    REPO_ROOT = find_repo_root()
"""
from __future__ import annotations

from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upwards from *start* until the GMS_Remote_Test_Web root is found.

    The root is identified by its structural markers (``pyproject.toml`` plus
    the ``features``/``worker_agent`` source trees), never by a fixed number
    of parent hops, so moving a script between ``tools/scripts/<category>/``
    directories cannot break it.

    Raises RuntimeError when no ancestor matches — e.g. when the script is
    copied out of the checkout.
    """
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "features").is_dir()
            and (candidate / "worker_agent").is_dir()
        ):
            return candidate
    raise RuntimeError("GMS_Remote_Test_Web repository root not found")
