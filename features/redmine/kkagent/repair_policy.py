"""Decode the declared file path of an opaque SDK result_id.

This is a consistency check; the SDK backend verifies the token HMAC.
"""
from __future__ import annotations

import base64
import json


def signed_source_path(result_id: str) -> str:
    if not result_id.startswith("src1_"):
        return ""
    encoded, separator, signature = result_id[5:].rpartition("_")
    if not separator or not signature or len(encoded) > 1600:
        return ""
    try:
        value = json.loads(base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8"))
        return str(value.get("path") or "") if isinstance(value, dict) else ""
    except (ValueError, TypeError, UnicodeError):
        return ""
