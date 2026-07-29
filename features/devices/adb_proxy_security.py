from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from foundation.config import settings


def pair_code_for_worker(
    source_worker_id: str,
    local_worker_id: str,
    grant: str,
) -> str:
    from worker_agent.adb_proxy import pair_code_from_grant

    secret = _source_secret(source_worker_id, local_worker_id)
    return pair_code_from_grant(secret, grant)


def create_pair_grant(
    source_worker_id: str,
    target_worker_id: str,
    local_worker_id: str,
    *,
    ttl_seconds: int = 90,
) -> str:
    expires_at = int(time.time()) + max(30, min(int(ttl_seconds), 300))
    payload = {
        "source": source_worker_id,
        "target": target_worker_id,
        "exp": expires_at,
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _urlsafe(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    secret = _source_secret(source_worker_id, local_worker_id)
    signature = _urlsafe(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def validate_pair_grant(
    grant: str,
    source_worker_id: str,
    target_worker_id: str,
    local_worker_id: str,
) -> None:
    try:
        encoded, supplied = grant.split(".", 1)
        payload = json.loads(_urlbytes(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid ADB Proxy access grant") from exc
    secret = _source_secret(source_worker_id, local_worker_id)
    expected = _urlsafe(
        hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("invalid ADB Proxy access grant")
    if payload.get("source") != source_worker_id or payload.get("target") != target_worker_id:
        raise ValueError("ADB Proxy grant host mismatch")
    try:
        expires_at = int(payload.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid ADB Proxy access grant") from exc
    if expires_at < int(time.time()) or expires_at > int(time.time()) + 300:
        raise ValueError("ADB Proxy access grant expired")


def _source_secret(source_worker_id: str, local_worker_id: str) -> bytes:
    from features.cluster.worker_auth import worker_tokens

    token = worker_tokens().get(source_worker_id)
    if token:
        return token.encode("utf-8")
    if source_worker_id != local_worker_id:
        raise ValueError(f"worker token is not configured for {source_worker_id}")
    return _local_secret()


def _local_secret() -> bytes:
    configured = os.getenv("GMS_ADB_PROXY_SECRET_FILE", "").strip()
    path = (
        Path(configured)
        if configured
        else settings.data_root / "secrets/adb_proxy.key"
    )
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise RuntimeError(f"ADB Proxy secret file permissions must be 0600: {path}")
        value = path.read_bytes().strip()
        if len(value) < 32:
            raise RuntimeError("ADB Proxy secret file is invalid")
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, value)
    finally:
        os.close(descriptor)
    return value


def local_proxy_secret() -> bytes:
    """Return the Controller-local Worker secret for proxy state recovery."""
    return _local_secret()


def _urlsafe(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlbytes(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
