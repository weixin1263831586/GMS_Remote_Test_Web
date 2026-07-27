"""Fail-closed production security configuration checks."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from features.auth import authentication_required, secure_cookies_enabled
from features.cluster import worker_tokens
from features.system import security_audit_logger
from features.system.metrics import metrics_token
from foundation.config import settings
from foundation.secrets import validate_secret_configuration


def _environment() -> str:
    return os.getenv("GMS_ENV", settings.environment).strip().lower()


def _require_secret(name: str, value: str, minimum: int = 32) -> None:
    if len(str(value or "").strip()) < minimum:
        raise RuntimeError(f"{name} must contain at least {minimum} characters")


def validate_production_security_configuration() -> None:
    """Validate every control-plane secret before accepting traffic."""

    if _environment() != "production":
        return
    if not authentication_required():
        raise RuntimeError("GMS_AUTH_REQUIRED cannot be disabled in production")
    if not secure_cookies_enabled():
        raise RuntimeError("GMS_SECURE_COOKIES cannot be disabled in production")

    validate_secret_configuration()
    security_audit_logger.validate_configuration()
    _require_secret("GMS_METRICS_TOKEN", metrics_token())
    _require_secret(
        "GMS_AUTOMATION_WEBHOOK_TOKEN",
        os.getenv("GMS_AUTOMATION_WEBHOOK_TOKEN", ""),
    )
    if not os.getenv("GMS_AUTOMATION_OWNER_ID", "").strip():
        raise RuntimeError("GMS_AUTOMATION_OWNER_ID is required in production")

    tokens = worker_tokens()
    if not tokens:
        raise RuntimeError(
            "worker tokens are required in production "
            "(configure configs/worker_tokens.json)"
        )
    for worker_id, token in tokens.items():
        if not worker_id.strip():
            raise RuntimeError("Worker token entries require a worker id")
        _require_secret(f"worker token for {worker_id}", token)

    origins = [
        item.strip()
        for item in os.getenv("GMS_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    ]
    if not origins:
        raise RuntimeError("GMS_ALLOWED_ORIGINS is required in production")
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
        ):
            raise RuntimeError(
                "GMS_ALLOWED_ORIGINS must contain exact HTTPS origins in production"
            )
