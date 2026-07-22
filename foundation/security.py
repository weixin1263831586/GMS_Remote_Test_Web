from __future__ import annotations

import re


def safe_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


# 设备序列号仅允许安全字符，禁止 Shell 元字符、空白和路径分隔符。
_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def is_safe_device_id(value: str | None) -> bool:
    """True when ``value`` is a safe device serial (no shell/path metacharacters).

    Device ids flow from the client into adb/scrcpy commands, SSH command
    strings, and log file names (``/tmp/scrcpy_<id>.log``). A value carrying
    ``;``/``$()``/backticks would be a remote command-injection vector, so any
    id that is not a strict alphanumeric serial must be rejected upstream.
    """
    return bool(value) and _DEVICE_ID_PATTERN.match(str(value)) is not None


def sanitize_device_ids(values) -> list[str]:
    """Return only the safe device ids from ``values``, dropping the rest.

    Callers that fan a device list out into shell/SSH commands should filter
    with this rather than trusting the raw request payload.
    """
    if not values:
        return []
    return [str(v) for v in values if is_safe_device_id(v)]
