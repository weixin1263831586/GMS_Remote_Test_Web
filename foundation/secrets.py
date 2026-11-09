"""Runtime secret encryption with production key injection."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from foundation.config import settings
from foundation.key_cache import cached_key, key_file_fingerprint
from foundation.runtime_settings import is_production_environment


logger = logging.getLogger(__name__)

_CONCURRENT_KEY_READ_ATTEMPTS = 50
_CONCURRENT_KEY_READ_DELAY_SECONDS = 0.01

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

    An empty file means a previous creator crashed between O_EXCL creation
    and the write; it never held key material, so it must not be treated as
    a published key — that state would otherwise hard-fail every startup
    forever while the bootstrap branch only runs when the file is absent.
    """
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _atomic_write_private_file(path: Path, payload: bytes) -> None:
    """Publish key material atomically: temp file + fsync + ``os.replace``.

    Mirrors ``private_config.write_private_json``. A crash mid-write must
    never leave a truncated key at ``path`` — that would permanently strand
    every secret encrypted under it. Concurrent publishers are safe when the
    payload is derived from the same source (identical bytes; ``os.replace``
    is atomic).
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


def _read_key_after_concurrent_create(path: Path) -> bytes:
    """Wait briefly for the process that won O_EXCL to finish its write."""
    last_error: Exception | None = None
    for attempt in range(_CONCURRENT_KEY_READ_ATTEMPTS):
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise RuntimeError(f"secret key file permissions must be 0600: {path}")
            return _validate_key(path.read_bytes())
        except (OSError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < _CONCURRENT_KEY_READ_ATTEMPTS:
                time.sleep(_CONCURRENT_KEY_READ_DELAY_SECONDS)
    raise RuntimeError(f"concurrently created secret key was not readable: {path}") from last_error


def _write_all(descriptor: int, payload: bytes) -> None:
    pending = memoryview(payload)
    while pending:
        written = os.write(descriptor, pending)
        if written <= 0:
            raise OSError("failed to write secret key")
        pending = pending[written:]


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
    if path == legacy_path and not _key_file_present(canonical_path):
        # Preserve existing deployments while moving the default key beside
        # the encrypted configuration it protects.  Publish atomically so a
        # crash can never leave a truncated key; concurrent migrators write
        # identical bytes read from the same legacy file.
        try:
            _atomic_write_private_file(canonical_path, legacy_path.read_bytes())
            path = canonical_path
        except OSError:
            # A read-only deployment can continue with the legacy key; the
            # next writable startup can complete the migration.
            pass
    if _key_file_present(path):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError(f"secret key file permissions must be 0600: {path}")
        return _validate_key(path.read_bytes())

    # 部署数据目录（data/）可能被整体重置。密钥文件缺失（或为空残留——
    # 创建者崩溃于写入前）时自举一个新的部署本地密钥，而不是启动即崩溃；
    # 生产模式下明确告警，提示旧密钥加密过的存量密文将无法解密。文件存
    # 在但内容损坏/权限错误仍按配置错误硬失败。
    if _production():
        logger.warning(
            "GMS master key file missing (%s); generated a new "
            "deployment-local key. Secrets encrypted under a previous "
            "key can no longer be decrypted.",
            path,
        )

    # 空文件不是密钥：先清除崩溃残留，否则下方 O_EXCL 永远命中既有空文件。
    try:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Fernet.generate_key()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # 并发启动的 Web/Worker 进程已抢先创建密钥：读取复用对方写入的
        # 密钥，而不是让其中一个进程启动即崩溃（与上方迁移分支一致）。
        return _read_key_after_concurrent_create(path)
    try:
        _write_all(descriptor, key + b"\n")
        # 密钥是后续所有密文的信任根：写入即落盘，避免断电留下半截文件。
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
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
