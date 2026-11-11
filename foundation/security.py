from __future__ import annotations

import hashlib
import hmac
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


def bearer_token(authorization: str | None) -> str | None:
    """Extract the credential from an ``Authorization: Bearer <token>`` header.

    Strict scheme parsing: a missing separator or a non-Bearer scheme yields
    ``None`` instead of silently accepting a bare token. Callers must still
    compare digests under :func:`secrets_equal` (hash both sides first so
    the comparison time cannot leak the stored token's length and non-ASCII
    input cannot raise ``TypeError``).
    """
    if not authorization:
        return None
    scheme, separator, supplied = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    supplied = supplied.strip()
    return supplied or None


def secrets_equal(left: str, right: str) -> bool:
    """Compare two secrets without leaking length or rejecting non-ASCII.

    ``hmac.compare_digest`` on ``str`` returns as soon as the lengths differ,
    and raises ``TypeError`` when either side contains a non-ASCII character.
    Hashing both sides first keeps the accept/reject result for ASCII secrets
    and turns a non-ASCII password or token into a normal mismatch instead of
    an unhandled 500.
    """

    left_digest = hashlib.sha256(str(left).encode("utf-8")).digest()
    right_digest = hashlib.sha256(str(right).encode("utf-8")).digest()
    return hmac.compare_digest(left_digest, right_digest)


def sanitize_device_ids(values) -> list[str]:
    """Return only the safe device ids from ``values``, dropping the rest.

    Callers that fan a device list out into shell/SSH commands should filter
    with this rather than trusting the raw request payload.
    """
    if not values:
        return []
    return [str(v) for v in values if is_safe_device_id(v)]


_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def has_control_chars(value: str | None) -> bool:
    """True when ``value`` contains ASCII control characters (incl. newline)."""
    return bool(value) and _CONTROL_CHAR_PATTERN.search(str(value)) is not None


def quote_device_shell_arg(value: str) -> str:
    """Single-quote one argument for the Android device shell (mksh/toybox).

    ``adb shell a b c`` joins its trailing args into one line that the *device*
    shell re-parses, so free-form values (WiFi SSID/password) carrying ``;``,
    ``$()`` or backticks would execute on the device. Wrapping each value in
    single quotes and escaping embedded quotes forces literal interpretation.
    """
    return "'" + str(value).replace("'", "'\\''") + "'"
