"""Shared redaction for credentials embedded in free-form text."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r'(?i)(\b(?:password|passwd|pswd|token|secret|api[_-]?key|apikey|authorization|cookie|pair_code)\b'
    r'["\']?\s*[:=]\s*["\']?)([^\s,;}&"\']+)'
)
_AUTHORIZATION_VALUE_PATTERN = re.compile(
    r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]+'
)
_OPENAI_STYLE_SECRET_PATTERN = re.compile(r'\bsk-[A-Za-z0-9_-]+\b')
_URL_CREDENTIAL_PATTERN = re.compile(r'(?i)(https?://)[^/@\s]+@')
_SENSITIVE_FIELD_PATTERN = re.compile(
    r'password|passwd|pswd|token|secret|api[_-]?key|authorization|cookie|pair[_-]?code|private[_-]?key',
    re.IGNORECASE,
)


def redact_sensitive_data(value: Any, *, known_values=(), mask: str = '***') -> Any:
    """Copy display data, masking secret fields and their occurrences in text."""
    secrets = {str(item) for item in known_values if item}

    def collect(item, secret=False):
        if isinstance(item, dict):
            for key, child in item.items():
                collect(child, secret or bool(_SENSITIVE_FIELD_PATTERN.search(str(key))))
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child, secret)
        elif secret and isinstance(item, (str, int)) and item:
            secrets.add(str(item))

    collect(value)
    ordered_secrets = sorted(secrets, key=len, reverse=True)

    def public(item):
        if isinstance(item, dict):
            return {key: (mask if child else '') if _SENSITIVE_FIELD_PATTERN.search(str(key))
                    else public(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [public(child) for child in item]
        if isinstance(item, str):
            for secret in ordered_secrets:
                item = item.replace(secret, mask)
            return redact_sensitive_text(item)
        return item

    return public(value)


def redact_sensitive_text(value: object) -> str:
    """Hide common credentials without changing the surrounding error text."""

    redacted = _URL_CREDENTIAL_PATTERN.sub(
        r'\1***REDACTED***@',
        str(value or ''),
    )
    redacted = _AUTHORIZATION_VALUE_PATTERN.sub(
        lambda match: f'{match.group(1)} ***REDACTED***',
        redacted,
    )
    redacted = _OPENAI_STYLE_SECRET_PATTERN.sub('***REDACTED***', redacted)
    return _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f'{match.group(1)}***REDACTED***',
        redacted,
    )
