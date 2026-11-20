#!/usr/bin/env python3
"""Self-follow guard for versioned MCP server installs.

A daemon-held MCP server is a singleton: it is spawned once and its
stdin/stdout pipes stay open across client sessions, so the process
outlives package upgrades. When ``current`` moves to a new
versions/<ver> tree the resident process keeps serving stale code —
bugs already fixed in the new version (for example the 30s CLI curl
timeout that truncated SDK repo-wide searches while the fix shipped
in 0.22.84) keep hitting every client until someone notices days
later.

The guard re-execs the same process in place, in the idle gap before
reading the next request: the daemon's pipes are untouched, the same
PID keeps serving, and the next request runs the version ``current``
declares. Repo checkouts (no versions/ + current layout) and failed
re-execs degrade to serving the loaded image with a stderr note —
never a dead singleton.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


CURRENT_LINK = "current"


def staleness(own_script: str) -> dict | None:
    """Return follow info when ``current`` points at another version.

    Layout: ``<root>/versions/<ver>/scripts/mcp_server.py`` plus a
    ``<root>/current`` symlink. Anything else (repo checkout, relocated
    plugin install) returns ``None`` — the guard must never guess.
    """
    try:
        script = Path(own_script).resolve(strict=True)
    except OSError:
        return None
    if script.name != "mcp_server.py" or script.parent.name != "scripts":
        return None
    version_dir = script.parent.parent        # .../versions/<ver>
    versions_dir = version_dir.parent         # .../versions
    if versions_dir.name != "versions":
        return None
    link = versions_dir.parent / CURRENT_LINK
    if not link.is_symlink():
        return None
    try:
        current_dir = link.resolve(strict=True)
    except OSError:
        return None
    target = current_dir / "scripts" / script.name
    if not target.is_file():
        return None
    own_version = version_dir.name
    current_version = current_dir.name
    if current_version == own_version:
        return None
    return {
        "own_version": own_version,
        "current_version": current_version,
        "target_script": str(target),
    }


def follow(
    info: dict,
    *,
    argv: list[str],
    executable: str | None = None,
    execv=os.execv,
    stderr=None,
) -> bool:
    """Re-exec into ``info["target_script"]``.

    ``execv``/``stderr`` are injectable for tests (stderr resolved at
    call time so capture fixtures see the note). On success this never
    returns (the process image is replaced); any failure keeps the old
    image alive and returns ``False`` so the server can keep serving.
    """
    command = [executable or sys.executable, info["target_script"], *argv[2:]]
    try:
        execv(command[0], command)
    except OSError as exc:
        print(
            f"version_guard: follow {info['current_version']} failed: {exc}; "
            f"keep serving {info['own_version']}",
            file=stderr or sys.stderr,
        )
        return False
