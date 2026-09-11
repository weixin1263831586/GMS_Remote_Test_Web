"""Optional Ed25519 signing for executable Skill archives."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SIGNING_KEY_ENV = "GMS_SKILL_SIGNING_KEY_FILE"

logger = logging.getLogger(__name__)


def _generate_signing_key(path: Path) -> Ed25519PrivateKey:
    """Bootstrap a fresh signing key when the configured file is missing.

    部署数据目录（data/）被重置后，配置仍指向旧路径时自举新密钥而非
    启动崩溃；生产模式下由调用方日志告警。文件存在但内容损坏仍按配置
    错误硬失败。
    """

    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, pem)
    finally:
        os.close(descriptor)
    logger.warning(
        "%s pointed at a missing file (%s); generated a new Ed25519 "
        "signing key. Agent packages signed with the previous key must be "
        "re-signed.",
        SIGNING_KEY_ENV,
        path,
    )
    return private_key


def _signing_key() -> Ed25519PrivateKey | None:
    configured = os.getenv(SIGNING_KEY_ENV, "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    if not path.exists():
        return _generate_signing_key(path)
    try:
        private_key = serialization.load_pem_private_key(
            path.read_bytes(),
            password=None,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{SIGNING_KEY_ENV} is not a readable PEM private key"
        ) from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise RuntimeError(
            f"{SIGNING_KEY_ENV} must contain an Ed25519 private key"
        )
    return private_key


def sign_skill_archive(content: bytes) -> str:
    """Return a base64 Ed25519 signature, or an empty string when disabled."""
    key = _signing_key()
    if key is None:
        return ""
    return base64.b64encode(key.sign(content)).decode("ascii")


def skill_verify_key_b64() -> str:
    """Return the configured signing key's public PEM as base64."""
    key = _signing_key()
    if key is None:
        return ""
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(public_pem).decode("ascii")
