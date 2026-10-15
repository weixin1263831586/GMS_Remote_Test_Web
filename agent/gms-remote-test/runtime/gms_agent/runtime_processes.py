"""Discover this user's Linux MCP runtimes without reading credentials."""

from __future__ import annotations

import os
from pathlib import Path


def running_runtimes(versions: Path, proc: Path = Path("/proc")) -> list[dict]:
    """Find exact versioned launcher/server argv paths, including deleted ones."""
    try:
        processes = list(proc.iterdir())
    except OSError:
        return []
    root = versions.resolve()
    result = []
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if len(argv) < 2:
                continue
            # The launcher execs [python, absolute-script, ...]. Never search
            # arbitrary arguments or command strings for a version substring.
            script = Path(os.fsdecode(argv[1]))
            relative = script.relative_to(root)
        except (OSError, ValueError):
            continue
        if (len(relative.parts) == 3 and relative.parts[1] == "scripts"
                and relative.name in {"mcp_server.py", "mcp_launcher.py"}):
            result.append({"pid": int(process.name), "version": relative.parts[0]})
    return sorted(result, key=lambda item: item["pid"])
