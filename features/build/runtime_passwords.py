"""Encrypted, TTL-bounded runtime SSH passwords for build servers.

Users may supply a one-shot SSH password to start a build against a
password-auth server. Keeping it only in a process-local dict meant:
- a different Uvicorn worker handling poll/cancel could not see it;
- a Controller restart lost it while a remote tmux build was still running,
  making poll/cancel impossible for password-auth servers.

Storing the plaintext in SQLite was rejected. This store keeps an encrypted
blob (Fernet via ``foundation.secrets``) on disk with a hard TTL, so one
process can start a build and another can poll it, without ever persisting
plaintext. Password-auth servers still cannot QUEUE (a build needs the
password at start time), so the TTL only needs to outlive a typical job.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from foundation.secrets import decrypt_secret, encrypt_secret


logger = logging.getLogger(__name__)


DEFAULT_RUNTIME_PASSWORD_TTL_SECONDS = 12 * 60 * 60


class RuntimePasswordStore:
    """File-backed encrypted store: job_id -> (encrypted password, expires_at)."""

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int = DEFAULT_RUNTIME_PASSWORD_TTL_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[str, float]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._cache = {}
            return
        except OSError as exc:
            logger.warning("Runtime password store unreadable (%s): %s", self.path, exc)
            self._cache = {}
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Runtime password store corrupted; ignoring: %s", self.path)
            self._cache = {}
            return
        now = time.time()
        self._cache = {
            str(key): (str(value.get("blob") or ""), float(value.get("expires_at") or 0))
            for key, value in (data or {}).items()
            if isinstance(value, dict) and float(value.get("expires_at") or 0) > now
        }

    def _persist(self) -> None:
        now = time.time()
        live = {
            key: {"blob": blob, "expires_at": expires}
            for key, (blob, expires) in self._cache.items()
            if expires > now
        }
        self._cache = live
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            try:
                os.write(
                    descriptor,
                    json.dumps(live).encode("utf-8"),
                )
            finally:
                os.close(descriptor)
        except OSError as exc:
            logger.warning("Runtime password store unwritable (%s): %s", self.path, exc)

    def set(self, job_id: str, password: str) -> None:
        if not job_id or not password:
            return
        with self._lock:
            self._load()
            self._cache[job_id] = (
                encrypt_secret(password),
                time.time() + self.ttl_seconds,
            )
            self._persist()

    def get(self, job_id: str) -> str:
        if not job_id:
            return ""
        with self._lock:
            self._load()
            entry = self._cache.get(job_id)
            if not entry:
                return ""
            blob, expires = entry
            if expires <= time.time():
                self._cache.pop(job_id, None)
                self._persist()
                return ""
        try:
            return decrypt_secret(blob)
        except RuntimeError:
            logger.warning("Runtime password for %s could not be decrypted", job_id)
            return ""

    def pop(self, job_id: str) -> None:
        if not job_id:
            return
        with self._lock:
            self._load()
            if self._cache.pop(job_id, None) is not None:
                self._persist()
