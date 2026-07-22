"""Exact host-scoped device SSH credential lookup."""

from __future__ import annotations

import logging
from typing import Any

from foundation.secrets import decrypt_secret


logger = logging.getLogger(__name__)


def find_device_host_password(
    device_host: str,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Resolve an encrypted credential for the exact user and host."""
    if not config or "@" not in device_host:
        return None
    username, hostname = (
        item.strip() for item in device_host.split("@", 1)
    )
    if not username or not hostname:
        return None
    for credential in config.get("client_ssh_credentials", []):
        if not isinstance(credential, dict):
            continue
        credential_host = str(credential.get("device_host") or "").strip()
        credential_username = str(credential.get("username") or "").strip()
        credential_hostname = str(
            credential.get("host") or credential.get("hostname") or ""
        ).strip()
        if credential_host != device_host and (
            credential_username != username or credential_hostname != hostname
        ):
            continue
        encrypted = str(credential.get("encrypted_password") or "")
        if not encrypted:
            if credential.get("password"):
                logger.warning(
                    "Ignoring plaintext SSH credential for %s; rotate it",
                    device_host,
                )
            return None
        try:
            return decrypt_secret(encrypted)
        except RuntimeError:
            logger.warning(
                "SSH credential for %s cannot be decrypted; rotate it",
                device_host,
            )
            return None
    return None
