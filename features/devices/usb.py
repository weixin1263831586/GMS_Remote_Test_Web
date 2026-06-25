from __future__ import annotations

import re
from collections.abc import Iterator


DEFAULT_ANDROID_USBIP_VID_PIDS = ('2207:0006',)
ANDROID_USBIP_MARKERS = ('android', 'adb', 'rk356', 'rockchip')
USBIPD_BUSID_RE = re.compile(r'^\d+(?:-\d+)+$')
ANSI_ESCAPE_RE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub('', text or '')


def _iter_connected_lines(output: str) -> Iterator[str]:
    in_connected = False
    saw_section = False
    for line in _strip_ansi(output).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('Connected:'):
            in_connected = True
            saw_section = True
            continue
        if stripped.startswith('Persisted:'):
            break
        if in_connected or (not saw_section and _looks_like_usbipd_device_line(stripped)):
            yield stripped


def _looks_like_usbipd_device_line(line: str) -> bool:
    parts = line.split()
    return bool(parts and USBIPD_BUSID_RE.match(parts[0]))


def parse_usbipd_android_busids(
    output: str,
    vid_pid: str | None = None,
) -> list[str]:
    vid_pids = {pid.lower() for pid in DEFAULT_ANDROID_USBIP_VID_PIDS}
    if vid_pid:
        vid_pids.add(vid_pid.lower())

    busids: list[str] = []
    for stripped in _iter_connected_lines(output):
        lowered = stripped.lower()
        if (
            any(pid in lowered for pid in vid_pids)
            or any(marker in lowered for marker in ANDROID_USBIP_MARKERS)
        ):
            parts = stripped.split()
            if parts and USBIPD_BUSID_RE.match(parts[0]):
                busids.append(parts[0])
    return busids


def parse_usbipd_busid_statuses(output: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for stripped in _iter_connected_lines(output):
        parts = stripped.split()
        if not parts or not USBIPD_BUSID_RE.match(parts[0]):
            continue
        lowered = stripped.lower()
        if 'not shared' in lowered:
            statuses[parts[0]] = 'not_shared'
        elif 'attached' in lowered:
            statuses[parts[0]] = 'attached'
        elif 'shared' in lowered:
            statuses[parts[0]] = 'shared'
        else:
            statuses[parts[0]] = 'unknown'
    return statuses
