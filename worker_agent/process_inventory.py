"""Read-only discovery of managed and manually launched Tradefed invocations.

The scanner deliberately uses ``/proc`` only.  It never adopts, signals, or
otherwise changes a process; its output is used solely for host accounting and
device admission control.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TRADEFED_LAUNCHER = re.compile(r"^(?:cts|gts|vts|sts)-tradefed$")
_SERIAL_OPTIONS = {"-s", "--serial", "--device-serial"}
_RUNTIME_INFO = re.compile(r"(?P<path>/[^\s:]+/tf_runtime_info)(?::|$)")
_RUNTIME_ACTIVITY = re.compile(r"(?P<path>/[^\s:]+/(?:tf_runtime_info|tf_test_module_results))(?::|$)")
_TEST_ID = re.compile(r"/test_(?P<id>[0-9a-f-]{20,})/")


def _read_cmdline(path: Path) -> list[str]:
    return [
        item.decode("utf-8", errors="replace")
        for item in path.read_bytes().split(b"\0")
        if item
    ]


def _read_stat(path: Path) -> dict[str, int]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # The comm field is parenthesized and may itself contain spaces.
    fields = raw[raw.rfind(")") + 2 :].split()
    return {
        "ppid": int(fields[1]),
        "pgid": int(fields[2]),
        "sid": int(fields[3]),
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "start_ticks": int(fields[19]),
    }


def _is_tradefed(argv: list[str], comm: str = "") -> bool:
    if _TRADEFED_LAUNCHER.fullmatch(comm.lower()):
        return True
    if any(_TRADEFED_LAUNCHER.fullmatch(Path(item).name.lower()) for item in argv):
        return True
    joined = " ".join(argv).lower()
    return "tradefed.jar" in joined or "compatibilityconsole" in joined


def _looks_like_serial(value: str) -> bool:
    """Reject values that are clearly not device serial numbers."""
    value = str(value or "").strip()
    if not value or value.startswith("-"):
        return False
    # Device serials never contain "/" (file paths like frida scripts).
    if "/" in value:
        return False
    return True


def _extract_devices(argv: list[str]) -> set[str]:
    devices: set[str] = set()
    for index, item in enumerate(argv[:-1]):
        if item in _SERIAL_OPTIONS and _looks_like_serial(argv[index + 1]):
            devices.add(argv[index + 1])
    joined = " ".join(argv)
    for match in _RUNTIME_INFO.finditer(joined):
        try:
            payload = json.loads(Path(match.group("path")).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        for invocation in payload.get("invocations", []):
            devices.update(
                str(item) for item in invocation.get("deviceIds", [])
                if item and _looks_like_serial(item)
            )
    return devices


def _suite_type(argv: list[str]) -> str:
    joined = " ".join(argv).lower()
    for name in ("cts", "gts", "vts", "sts"):
        if f"{name}-tradefed" in joined or f"android-{name}" in joined:
            return name.upper()
    return "XTS"


def _find_log_path(argv: list[str], cwd: str) -> Path | None:
    for item in argv:
        if item.endswith(".log"):
            candidate = Path(item).expanduser()
            if candidate.is_file():
                return candidate
    joined = " ".join(argv)
    test_match = _TEST_ID.search(joined)
    cwd_path = Path(cwd) if cwd else None
    if test_match and cwd_path and cwd_path.name == "tools":
        logs = cwd_path.parent / "logs"
        pattern = f"*/*/TradefedTest_test_{test_match.group('id')}/xts_tf_output.log"
        try:
            return next((path for path in logs.glob(pattern) if path.is_file()), None)
        except OSError:
            return None
    return None


def _has_ancestor(processes: dict[int, dict[str, Any]], pid: int, ancestors: set[int]) -> bool:
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        if pid in ancestors:
            return True
        seen.add(pid)
        pid = int(processes.get(pid, {}).get("ppid", 0))
    return False


def _is_active_invocation(argv: list[str]) -> bool:
    """Exclude an idle Tradefed/ATS console that has no running invocation."""
    joined = " ".join(argv)
    return bool(_RUNTIME_INFO.search(joined) or re.search(r"\bCompatibilityConsole\s+run\b", joined))


def _collect_adb_descendant_argv(
    processes: dict[int, dict[str, Any]], group_pids: set[int],
) -> list[str]:
    """Return argv tokens from adb processes descended from a Tradefed group.

    When Tradefed runs interactively from its console (e.g. the user typed
    ``run vts`` at the ``vts >`` prompt), the invocation executes in-process
    without spawning a child JVM, so neither ``CompatibilityConsole run`` nor
    ``tf_runtime_info`` appears on the command line.  A live ``adb`` child,
    however, signals active device interaction.
    """
    argv: list[str] = []
    for pid, item in processes.items():
        if pid in group_pids:
            continue
        if not item.get("comm", "").startswith("adb"):
            continue
        if _has_ancestor(processes, pid, group_pids):
            argv.extend(item["argv"])
    return argv


def discover_tradefed_processes(
    managed_jobs: list[dict[str, Any]] | None = None,
    *,
    proc_root: Path = Path("/proc"),
    now: float | None = None,
    stall_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Return one record per external Tradefed invocation.

    Wrapper, console-server and invocation JVM processes are folded into the
    highest Tradefed ancestor.  Groups descending from an Agent-managed PID are
    omitted because ``WorkerRuntime.running_jobs`` already reports them.
    """

    now = time.time() if now is None else now
    stall_seconds = stall_seconds or int(os.getenv("GMS_TF_STALL_SECONDS", "3600"))
    try:
        uptime = float((proc_root / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = 0.0
    clock_ticks = max(1, int(os.sysconf("SC_CLK_TCK")))
    processes: dict[int, dict[str, Any]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = _read_stat(entry / "stat")
            argv = _read_cmdline(entry / "cmdline")
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, ValueError, IndexError):
            continue
        if not argv:
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        rss_kb = 0
        try:
            for line in (entry / "status").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            pass
        processes[pid] = {"pid": pid, "argv": argv, "comm": comm, "cwd": cwd,
                          "rss_kb": rss_kb, **stat}

    matched = {pid for pid, item in processes.items() if _is_tradefed(item["argv"], item["comm"])}
    groups: dict[int, list[dict[str, Any]]] = {}
    for pid in matched:
        root = pid
        parent = int(processes[root]["ppid"])
        seen: set[int] = set()
        while parent > 1 and parent not in seen:
            seen.add(parent)
            if parent in matched:
                root = parent
            parent = int(processes.get(parent, {}).get("ppid", 0))
        groups.setdefault(root, []).append(processes[pid])

    managed_pids = {
        int(item["pid"])
        for item in (managed_jobs or [])
        if item.get("pid") is not None
    }
    result: list[dict[str, Any]] = []
    for root_pid, members in groups.items():
        if any(_has_ancestor(processes, item["pid"], managed_pids) for item in members):
            continue
        argv = [token for member in members for token in member["argv"]]
        if not _is_active_invocation(argv):
            # Fall back: a Tradefed Console invoked interactively (user
            # typed "run vts" at the prompt) runs the invocation in-process
            # without spawning a child JVM, so argv lacks both
            # "CompatibilityConsole run" and "tf_runtime_info".  Detect
            # active device interaction via adb descendant processes.
            group_pids = {root_pid} | {item["pid"] for item in members}
            adb_argv = _collect_adb_descendant_argv(processes, group_pids)
            if not adb_argv:
                continue
            argv.extend(adb_argv)
        devices = sorted(_extract_devices(argv))
        start_ticks = min(item["start_ticks"] for item in members)
        elapsed = max(0.0, uptime - start_ticks / clock_ticks) if uptime else 0.0
        cpu_ticks = sum(item["cpu_ticks"] for item in members)
        cpu_percent = (100.0 * cpu_ticks / clock_ticks / elapsed) if elapsed > 0 else 0.0
        root = processes[root_pid]
        log_path = _find_log_path(argv, root.get("cwd", ""))
        log_age: float | None = None
        if log_path is not None:
            try:
                log_age = max(0.0, now - log_path.stat().st_mtime)
            except OSError:
                log_path = None
        activity_mtimes = []
        for match in _RUNTIME_ACTIVITY.finditer(" ".join(argv)):
            try:
                activity_mtimes.append(Path(match.group("path")).stat().st_mtime)
            except OSError:
                continue
        if activity_mtimes:
            activity_age = max(0.0, now - max(activity_mtimes))
            log_age = min(log_age, activity_age) if log_age is not None else activity_age
        warning = ""
        if not devices:
            warning = "Tradefed is running but its device could not be identified"
        elif log_age is not None and log_age >= stall_seconds:
            warning = (
                f"Tradefed output has been inactive for {int(log_age)} seconds; "
                "the current module may be long-running or stalled"
            )
        started_at = ""
        if elapsed:
            started_at = datetime.fromtimestamp(now - elapsed, timezone.utc).isoformat()
        command = " ".join(root["argv"])
        result.append({
            "worker_job_id": f"external-{root_pid}-{start_ticks}",
            "job_id": "",
            "attempt_id": "",
            "status": "running",
            "pid": root_pid,
            "devices": devices,
            "source": "external",
            "suite_type": _suite_type(argv),
            "command": command[:1000],
            "started_at": started_at,
            "elapsed_seconds": int(elapsed),
            "cpu_percent": round(cpu_percent, 2),
            "rss_mb": round(sum(item["rss_kb"] for item in members) / 1024, 2),
            "process_count": len(members),
            "log_path": str(log_path) if log_path else "",
            "last_output_age_seconds": int(log_age) if log_age is not None else None,
            "warning": warning,
        })
    return sorted(result, key=lambda item: (item["started_at"], item["pid"]))
