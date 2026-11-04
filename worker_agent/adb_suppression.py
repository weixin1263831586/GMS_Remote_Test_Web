"""Suppress the host adb daemon while an AOA HID session owns the USB device.

Entering accessory mode re-enumerates the device; with USB debugging
enabled it comes back as a composite ``18d1:2D0x`` accessory whose
interfaces include an adb port. The host adb daemon then races the pyusb
AOA executor for the same USB device: it connects, resets or retrains the
interface in the middle of HID control transfers until the gadget stack
gives up (observed on RK3576: ``usbfs: process (adb) did not claim
interface 1`` → xhci reset storm → ``device vanished`` mid-playback).

While at least one AOA session is active on this host:

- the Worker app and the Controller's LocalWorkerBridge skip adb device
  enumeration (fastboot / usb_only probing is unaffected);
- the adb server is killed once when the first session starts and only
  restarts after the last session ends, so AOA's own ``_wait_adb_state``
  (which runs after the session) still observes adb recovery.

Dispatchers must only arm the suppression while no Tradefed attempt and
no ADB Proxy import needs the adb server (see
``features/cluster/device_action_operations.py`` and
``worker_agent/app.py``); killing the server mid-test would break device
communication for unrelated running jobs.

The state is exposed across processes on the same host with one flag file
per suppressing process (``...-<uid>-<pid>.flag``) and a TTL that outlives
the longest permitted AOA operation, covering a Worker process that dies
before its cleanup runs. Per-process flags keep concurrent suppressors
independent: a process that finishes its own sessions unlinks only its own
flag and never revokes another live process's suppression (the Worker app
and the Controller's LocalWorkerBridge run side by side on the same host).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from foundation.aoa_profile import MAX_AOA_OPERATION_SECONDS


# Cross-process flag TTL: one AOA session may legitimately run up to
# MAX_AOA_OPERATION_SECONDS; anything older than that is a dead session.
FLAG_TTL_SECONDS = MAX_AOA_OPERATION_SECONDS + 120

_LOCK = threading.Lock()
_SESSIONS = 0


def _flag_path(pid: int | None = None) -> Path:
    root = os.getenv("GMS_WORKER_AOA_SUPPRESSION_DIR") or tempfile.gettempdir()
    uid = getattr(os, "getuid", lambda: 0)()
    return Path(root) / f"gms-aoa-adb-suppression-{uid}-{pid if pid is not None else os.getpid()}.flag"


def _suppression_dir() -> Path:
    root = os.getenv("GMS_WORKER_AOA_SUPPRESSION_DIR") or tempfile.gettempdir()
    return Path(root)


def adb_daemon_suppressed(now: float | None = None) -> bool:
    """True while an AOA session holds the host adb daemon (cross-process).

    Process-internal sessions short-circuit; otherwise any fresh
    per-PID flag (this process included) means another live session.
    """
    with _LOCK:
        if _SESSIONS > 0:
            return True
    current = time.time() if now is None else now
    uid = getattr(os, "getuid", lambda: 0)()
    try:
        flags = list(_suppression_dir().glob(f"gms-aoa-adb-suppression-{uid}-*.flag"))
    except OSError:
        return False
    for flag in flags:
        try:
            if (current - flag.stat().st_mtime) <= FLAG_TTL_SECONDS:
                return True
        except OSError:
            continue
    return False


def _kill_adb_server() -> None:
    try:
        subprocess.run(
            ["adb", "kill-server"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return


class AoaAdbSuppression:
    """Session handle returned by ``begin_suppression``; ``end`` is idempotent."""

    def __init__(self, on_end) -> None:
        self._on_end = on_end
        self._ended = False

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._on_end()


def begin_suppression() -> AoaAdbSuppression:
    """Open one suppression session; returns a handle ending it again.

    On the first concurrent session of this process the adb server is
    killed so it does not race the accessory-mode enumeration that
    follows, and this process's flag file is written. Sessions from other
    processes keep their own flags; nothing here revokes them.
    """
    global _SESSIONS

    with _LOCK:
        _SESSIONS += 1
        first = _SESSIONS == 1
    if first:
        try:
            flag = _flag_path()
            flag.parent.mkdir(parents=True, exist_ok=True)
            # Overwriting a stale flag from a crashed session of the same
            # pid is safe: the TTL only bounds reads while no live session
            # rewrites it. Other processes' flags are never touched here.
            flag.write_text(str(os.getpid()), encoding="ascii")
        except OSError:
            pass
        _kill_adb_server()

    def _end_session() -> None:
        global _SESSIONS
        with _LOCK:
            _SESSIONS = max(0, _SESSIONS - 1)
            last = _SESSIONS == 0
        if last:
            # Only unlink this process's own flag: a concurrent process
            # may still hold live sessions whose suppression must survive.
            try:
                _flag_path().unlink()
            except OSError:
                pass

    return AoaAdbSuppression(_end_session)
