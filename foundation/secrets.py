"""Runtime secret encryption with production key injection."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from foundation.config import settings
from foundation.runtime_settings import is_production_environment


logger = logging.getLogger(__name__)


def _production() -> bool:
    return is_production_environment()


def _validate_key(raw: bytes) -> bytes:
    candidate = raw.strip()
    try:
        Fernet(candidate)
    except Exception as exc:
        raise RuntimeError("GMS secret key must be a valid Fernet key") from exc
    return candidate


def _key_path() -> Path:
    configured = os.getenv("GMS_SECRET_KEY_FILE", "").strip()
    if configured:
        return Path(configured)

    # Secrets under configs/secrets must survive cleanup/recreation of the
    # runtime data directory.  Keep the data-root location as a legacy
    # fallback so existing deployments continue using their current key.
    canonical = settings.project_root / "configs/secrets/master.key"
    legacy = settings.data_root / "secrets/master.key"
    if canonical.exists() or not legacy.exists():
        return canonical
    return legacy


def _load_key() -> bytes:
    injected = os.getenv("GMS_SECRET_KEY", "").strip()
    if injected:
        return _validate_key(injected.encode("ascii"))

    path = _key_path()
    legacy_path = settings.data_root / "secrets/master.key"
    canonical_path = settings.project_root / "configs/secrets/master.key"
    if path == legacy_path and not canonical_path.exists():
        # Preserve existing deployments while moving the default key beside
        # the encrypted configuration it protects.  Use exclusive creation
        # so two Web/Worker processes cannot overwrite each other's key.
        try:
            canonical_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                canonical_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(descriptor, legacy_path.read_bytes())
            finally:
                os.close(descriptor)
            path = canonical_path
        except FileExistsError:
            path = canonical_path
        except OSError:
            # A read-only deployment can continue with the legacy key; the
            # next writable startup can complete the migration.
            pass
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError(f"secret key file permissions must be 0600: {path}")
        return _validate_key(path.read_bytes())

    # 部署数据目录（data/）可能被整体重置。密钥文件缺失时自举一个新
    # 的部署本地密钥，而不是启动即崩溃；生产模式下明确告警，提示旧密
    # 钥加密过的存量密文将无法解密。文件存在但损坏/权限错误仍按配置
    # 错误硬失败。
    if _production():
        logger.warning(
            "GMS master key file missing (%s); generated a new "
            "deployment-local key. Secrets encrypted under a previous "
            "key can no longer be decrypted.",
            path,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, key + b"\n")
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
    purpose label, e.g. ``derive_application_key(b"gms-sdk-source-v1:")``.
    """

    purpose_bytes = purpose.encode("ascii") if isinstance(purpose, str) else bytes(purpose)
    return purpose_bytes + _load_key()
