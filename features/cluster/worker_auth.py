"""Worker token parsing, private persistence, and request authentication.

Tokens are stored exclusively in a separate ``0600`` JSON file.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from foundation.config_paths import worker_tokens_path
from foundation.security import bearer_token


_token_lock = threading.RLock()


class WorkerTokenStoreError(RuntimeError):
    """The token file exists but cannot be parsed; callers must not replace it."""


@contextmanager
def _exclusive_token_lock(path: Path):
    """Serialize token updates across Web, Worker and deploy processes.

    The in-process lock does not cover a second interpreter. Without a file
    lock, a revoke can read the map, a deploy can read the same map, and the
    later write puts the revoked token back.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _worker_tokens_path() -> Path:
    configured = os.getenv("GMS_WORKER_TOKENS_FILE", "").strip()
    if configured:
        return Path(configured)
    return worker_tokens_path(Path(__file__).resolve().parents[2])


def _read_token_document(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerTokenStoreError(
            f"worker token file is unreadable ({path}); refusing to continue"
        ) from exc
    if not isinstance(data, dict):
        raise WorkerTokenStoreError(
            f"worker token file must contain a JSON object ({path}); refusing to continue"
        )
    return data


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
    path = _worker_tokens_path()
    # A missing file is an empty map. Do not create the parent: production
    # startup probes a path that may not exist, and a read must not mkdir.
    if not path.exists():
        return {}
    with _token_lock, _exclusive_token_lock(path):
        return _token_map(_read_token_document(path))


def write_worker_tokens(tokens: dict[str, str]) -> None:
    # test seam: exercised only by features/cluster/tests/test_worker_auth.py
    """Persist the worker→token map in the dedicated private file."""
    normalized = {
        str(key): str(value)
        for key, value in tokens.items()
        if str(value)
    }
    path = _worker_tokens_path()
    with _token_lock, _exclusive_token_lock(path):
        _write_private_json(
            path,
            {"worker_tokens": dict(sorted(normalized.items()))},
        )


def persist_worker_token(worker_id: str, token: str) -> str | None:
    """Atomically set one Worker token and return its previous value."""
    path = _worker_tokens_path()
    with _token_lock, _exclusive_token_lock(path):
        tokens = _token_map(_read_token_document(path))
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
    path = _worker_tokens_path()
    with _token_lock, _exclusive_token_lock(path):
        tokens = _token_map(_read_token_document(path))
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
    path = _worker_tokens_path()
    with _token_lock, _exclusive_token_lock(path):
        tokens = _token_map(_read_token_document(path))
        if worker_id not in tokens:
            return False
        tokens.pop(worker_id)
        _write_private_json(
            path,
            {"worker_tokens": dict(sorted(tokens.items()))},
        )
        return True


def authenticate_worker(worker_id: str, authorization: str | None) -> None:
    supplied = bearer_token(authorization)
    try:
        expected = worker_tokens().get(worker_id)
    except WorkerTokenStoreError as exc:
        # 文件损坏与令牌错误必须区分：前者是依赖不可用，重试登录没有意义。
        raise HTTPException(503, "worker token store unavailable") from exc
    # 统一 401：「该 worker 未配置令牌」与「令牌错误」对未认证调用方必须
    # 不可区分，否则可用于枚举哪些 worker_id 已配置（worker_id 回显同理）。
    # 摘要后常量时间比较，不泄露存储令牌长度。
    supplied_digest = hashlib.sha256((supplied or "").encode()).digest()
    expected_digest = hashlib.sha256((expected or "").encode()).digest()
    matches = hmac.compare_digest(supplied_digest, expected_digest)
    if not supplied or not expected or not matches:
        raise HTTPException(401, "invalid worker token")
