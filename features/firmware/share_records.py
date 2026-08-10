"""Private credential handling and public views for firmware shares."""

from __future__ import annotations

from typing import Any

from foundation.secrets import decrypt_secret


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "name",
        "host",
        "user",
        "path",
        "filename",
        "size",
        "mtime",
        "created_at",
        "created_by",
        "expires_at",
        "downloads",
        "last_downloaded_at",
    }
    public = {key: record.get(key) for key in allowed if key in record}
    public["has_password"] = bool(
        record.get("password") or record.get("password_encrypted")
    )
    return public


def record_password(record: dict[str, Any]) -> str | None:
    """Resolve a share's SSH password (encrypted or legacy plaintext)."""
    encrypted = record.get("password_encrypted")
    if encrypted:
        try:
            return decrypt_secret(encrypted) or None
        except RuntimeError:
            return None
    return record.get("password") or None
