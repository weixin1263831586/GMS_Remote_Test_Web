"""Resolve the single canonical adb binary for this host.

adb clients refuse (and kill) a shared server started by a different
platform-tools version. The service PATH and interactive login-shell PATH
frequently disagree on which platform-tools comes first, so one component
may resolve SDK A while the terminal pty (``bash -l``) resolves SDK B —
every poll then flip-flops the shared adb server and instantly kills any
in-flight ``adb shell`` session (浏览器侧表现为「ADB Shell 启动失败」).

Pin ``GMS_ADB_PATH`` in the runtime environment
(``configs/local/environment.json``) so every component — device polling,
terminal live probe, and the local ADB terminal handshake — launches the
exact same client version.
"""

from __future__ import annotations

import os
import shutil


def adb_binary() -> str:
    """Return the pinned or PATH-resolved adb binary path.

    A pinned ``GMS_ADB_PATH`` must exist and be executable — a broken pin
    fails loudly instead of silently flip-flopping server versions again.
    """

    pinned = os.getenv("GMS_ADB_PATH", "").strip()
    if pinned:
        if os.path.isfile(pinned) and os.access(pinned, os.X_OK):
            return pinned
        raise RuntimeError(
            f"GMS_ADB_PATH={pinned!r} 不可执行（需要有效的 platform-tools adb）"
        )
    found = shutil.which("adb")
    if found:
        return found
    raise RuntimeError("adb not found on PATH (需要 Android platform-tools)")
