"""Runtime secret encryption with production key injection."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from foundation.config import settings
from foundation.runtime_settings import is_production_environment


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
    return Path(configured) if configured else settings.data_root / "secrets/master.key"


def _load_key() -> bytes:
    injected = os.getenv("GMS_SECRET_KEY", "").strip()
    if injected:
        return _validate_key(injected.encode("ascii"))

    path = _key_path()
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError(f"secret key file permissions must be 0600: {path}")
        return _validate_key(path.read_bytes())

    if _production():
        raise RuntimeError(
            "GMS_SECRET_KEY or a mode-0600 GMS_SECRET_KEY_FILE is required in production"
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
