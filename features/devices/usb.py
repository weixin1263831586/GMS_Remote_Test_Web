from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any


# Rockchip devices re-enumerate with different USB identities while crossing
# ADB, bootloader Fastboot and Loader modes. Each mode has its own VID:PID:
DEFAULT_ANDROID_USBIP_VID_PID_ADB = '2207:0006'        # ADB mode
DEFAULT_ANDROID_USBIP_VID_PID_FASTBOOT = '18d1:4d00'   # Fastboot / download mode
DEFAULT_ANDROID_USBIP_VID_PID_LOADER = '2207:351a'     # RockUSB Loader mode (RK3572)
DEFAULT_ANDROID_USBIP_VID_PIDS = (
    DEFAULT_ANDROID_USBIP_VID_PID_ADB,
    DEFAULT_ANDROID_USBIP_VID_PID_FASTBOOT,
    DEFAULT_ANDROID_USBIP_VID_PID_LOADER,
)
ANDROID_USBIP_MARKERS = (
    'android',
    'adb',
    'fastboot',
    'usb download gadget',
    'rockusb',
    'rk356',
    'rockchip',
)
USBIPD_BUSID_RE = re.compile(r'^\d+(?:-\d+)+$')
ANSI_ESCAPE_RE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
VID_PID_RE = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$')


def normalize_usbip_vid_pids(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalize one or more configured USB VID:PID values."""
    if value is None:
        return ()
    raw_items = re.split(r'[,;\s]+', value) if isinstance(value, str) else value
    normalized: list[str] = []
    for item in raw_items:
        candidate = str(item or '').strip().lower()
        if VID_PID_RE.fullmatch(candidate) and candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def configured_usbip_vid_pids(config: dict[str, Any]) -> tuple[str, ...]:
    """Resolve the plural config while retaining legacy single-value support.

    ``usbip_vid_pids`` is authoritative when present. Deployments that still
    have only ``usbip_vid_pid`` inherit the built-in Android mode defaults and
    keep their optional custom value, so an upgrade does not lose Fastboot.
    """
    if 'usbip_vid_pids' in config:
        configured = normalize_usbip_vid_pids(config.get('usbip_vid_pids'))
        return configured or DEFAULT_ANDROID_USBIP_VID_PIDS
    legacy = normalize_usbip_vid_pids(config.get('usbip_vid_pid'))
    return tuple(dict.fromkeys((*DEFAULT_ANDROID_USBIP_VID_PIDS, *legacy)))


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
    vid_pid: str | Iterable[str] | None = None,
) -> list[str]:
    configured = normalize_usbip_vid_pids(vid_pid)
    accepted_vid_pids = set(configured or DEFAULT_ANDROID_USBIP_VID_PIDS)

    busids: list[str] = []
    for stripped in _iter_connected_lines(output):
        lowered = stripped.lower()
        if (
            any(pid in lowered for pid in accepted_vid_pids)
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
