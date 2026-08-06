"""Worker token parsing, private persistence, and request authentication.

Tokens are stored exclusively in a separate ``0600`` JSON file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path

from fastapi import HTTPException


_token_lock = threading.RLock()


def _worker_tokens_path() -> Path:
    configured = os.getenv("GMS_WORKER_TOKENS_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "configs" / "worker_tokens.json"


def _read_token_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _token_map(raw: dict) -> dict[str, str]:
    tokens = raw.get("worker_tokens") if isinstance(raw, dict) else None
    if not isinstance(tokens, dict):
        return {}
    return {str(k): str(v) for k, v in tokens.items() if v}


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def worker_tokens() -> dict[str, str]:
    """Return the worker→token map from the dedicated private file."""
    with _token_lock:
        return _token_map(_read_token_raw(_worker_tokens_path()))


def write_worker_tokens(tokens: dict[str, str]) -> None:
    """Persist the worker→token map in the dedicated private file."""
    normalized = {
        str(key): str(value)
        for key, value in tokens.items()
        if str(value)
    }
    with _token_lock:
        _write_private_json(
            _worker_tokens_path(),
            {"worker_tokens": dict(sorted(normalized.items()))},
        )


def persist_worker_token(worker_id: str, token: str) -> str | None:
    """Atomically set one Worker token and return its previous value."""
    with _token_lock:
        path = _worker_tokens_path()
        tokens = _token_map(_read_token_raw(path))
        previous = tokens.get(worker_id)
        tokens[worker_id] = token
        _write_private_json(
            path,
            {"worker_tokens": dict(sorted(tokens.items()))},
        )
        return previous


def restore_worker_token(
    worker_id: str,
    installed_token: str,
    previous_token: str | None,
) -> bool:
    """Roll back one deployment token without overwriting a newer deploy."""
    with _token_lock:
        path = _worker_tokens_path()
        tokens = _token_map(_read_token_raw(path))
        if tokens.get(worker_id) != installed_token:
            return False
        if previous_token:
            tokens[worker_id] = previous_token
        else:
            tokens.pop(worker_id, None)
        _write_private_json(
            path,
            {"worker_tokens": dict(sorted(tokens.items()))},
        )
        return True


def revoke_worker_token(worker_id: str) -> bool:
    """Atomically revoke one Worker token while preserving all peers."""
    with _token_lock:
        path = _worker_tokens_path()
        tokens = _token_map(_read_token_raw(path))
        if worker_id not in tokens:
            return False
        tokens.pop(worker_id)
        _write_private_json(
            path,
            {"worker_tokens": dict(sorted(tokens.items()))},
        )
        return True


def authenticate_worker(worker_id: str, authorization: str | None) -> None:
    expected = worker_tokens().get(worker_id)
    if not expected:
        raise HTTPException(503, f"worker token is not configured for {worker_id}")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    ):
        raise HTTPException(401, "invalid worker token")
