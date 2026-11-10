"""Runtime secret encryption with production key injection."""

from __future__ import annotations

import fcntl
import logging
import os
import stat
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from foundation.config import settings
from foundation.key_cache import cached_key, key_file_fingerprint
from foundation.runtime_settings import is_production_environment


logger = logging.getLogger(__name__)

# File identity, timestamps and permissions invalidate keys after rotation.
_KEY_CACHE: dict[str, tuple[object, bytes]] = {}
_KEY_CACHE_LOCK = threading.Lock()


def _key_file_fingerprint(path: Path) -> tuple[int, ...] | None:
    return key_file_fingerprint(path)


def _cached_key(
    cache_key: str,
    fingerprint: Callable[[], object],
    load: Callable[[], bytes],
) -> bytes:
    return cached_key(_KEY_CACHE, _KEY_CACHE_LOCK, cache_key, fingerprint, load)


def _production() -> bool:
    return is_production_environment()


def _validate_key(raw: bytes) -> bytes:
    candidate = raw.strip()
    try:
        Fernet(candidate)
    except Exception as exc:
        raise RuntimeError("GMS secret key must be a valid Fernet key") from exc
    return candidate


def _key_file_present(path: Path) -> bool:
    """True when the key file exists with real content.

    Empty files from older interrupted creators are recovered only while
    holding the publication lock. New creators publish complete files.
    """
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _atomic_write_private_file(path: Path, payload: bytes) -> None:
    """Publish key material atomically: temp file + fsync + ``os.replace``.

    Callers must hold ``_key_publication_lock`` across the existence check
    and publication so a concurrent creator cannot replace a published key.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _key_path() -> Path:
    configured = os.getenv("GMS_SECRET_KEY_FILE", "").strip()
    if configured:
        return Path(configured)

    # Secrets under configs/secrets must survive cleanup/recreation of the
    # runtime data directory.  Keep the data-root location as a legacy
    # fallback so existing deployments continue using their current key.
    # An empty file counts as absent (crashed creator) so a half-done
    # migration falls back to the legacy key and can be redone.
    canonical = settings.project_root / "configs/secrets/master.key"
    legacy = settings.data_root / "secrets/master.key"
    if _key_file_present(canonical) or not _key_file_present(legacy):
        return canonical
    return legacy


@contextmanager
def _key_publication_lock(path: Path):
    """Serialize bootstrap and migration across Web, Worker and CLI processes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read_private_key(path: Path) -> bytes:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"secret key file permissions must be 0600: {path}")
    return _validate_key(path.read_bytes())


def _load_key() -> bytes:
    injected = os.getenv("GMS_SECRET_KEY", "").strip()
    if injected:
        return _cached_key(
            "env:GMS_SECRET_KEY",
            lambda: injected,
            lambda: _validate_key(injected.encode("ascii")),
        )

    path = _key_path()
    return _cached_key(
        f"file:{path}",
        lambda: _key_file_fingerprint(path),
        lambda: _load_key_from_file(path),
    )


def _load_key_from_file(path: Path) -> bytes:
    legacy_path = settings.data_root / "secrets/master.key"
    canonical_path = settings.project_root / "configs/secrets/master.key"
    if path == legacy_path:
        try:
            with _key_publication_lock(canonical_path):
                if not _key_file_present(canonical_path):
                    key = _read_private_key(legacy_path)
                    _atomic_write_private_file(canonical_path, key + b"\n")
            path = canonical_path
        except OSError:
            # A read-only deployment can continue with the legacy key; the
            # next writable startup can complete the migration.
            pass
    if _key_file_present(path):
        return _read_private_key(path)

    with _key_publication_lock(path):
        if _key_file_present(path):
            return _read_private_key(path)
        key = Fernet.generate_key()
        _atomic_write_private_file(path, key + b"\n")
        if _production():
            logger.warning(
                "GMS master key file missing (%s); generated a new "
                "deployment-local key. Secrets encrypted under a previous "
                "key can no longer be decrypted.",
                path,
            )
        return key


def encrypt_secret(value: str) -> str:
    return Fernet(_load_key()).encrypt(str(value or "").encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return Fernet(_load_key()).decrypt(str(value).encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("stored secret cannot be decrypted with the active key") from exc


def validate_secret_configuration() -> None:
    """Fail early when the configured encryption key is absent or invalid."""

    _load_key()


def derive_application_key(purpose: str) -> bytes:
    """Derive a stable per-purpose key from the deployment master secret.

    Public API for composition roots: callers must not know how the master
    key is loaded (env injection vs key file), only that keys derived here
    survive restarts and stay namespaced per ``purpose``. Use a short ASCII
    purpose label, e.g. ``derive_application_key("gms-sdk-source-v1")``.

    HKDF-SHA256 output is bound to ``purpose`` via the ``info`` field, so a
    derived key reveals nothing about the master key (a previous concat
    implementation leaked it: stripping the known prefix restored the key
    that also protects ``configs/secrets``).
    """

    purpose_bytes = purpose.encode("ascii") if isinstance(purpose, str) else bytes(purpose)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=purpose_bytes,
    ).derive(_load_key())
