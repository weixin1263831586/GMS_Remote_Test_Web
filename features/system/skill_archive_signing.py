"""Optional Ed25519 signing for executable Skill archives."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SIGNING_KEY_ENV = "GMS_SKILL_SIGNING_KEY_FILE"


def _signing_key() -> Ed25519PrivateKey | None:
    configured = os.getenv(SIGNING_KEY_ENV, "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
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
