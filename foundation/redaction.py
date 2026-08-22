"""Shared redaction for credentials embedded in free-form text."""

from __future__ import annotations

import re


_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r'(?i)(\b(?:password|passwd|pswd|token|secret|api[_-]?key|apikey|authorization|cookie|pair_code)\b'
    r'["\']?\s*[:=]\s*["\']?)([^\s,;}&"\']+)'
)
_AUTHORIZATION_VALUE_PATTERN = re.compile(
    r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]+'
)
_OPENAI_STYLE_SECRET_PATTERN = re.compile(r'\bsk-[A-Za-z0-9_-]+\b')
_URL_CREDENTIAL_PATTERN = re.compile(r'(?i)(https?://)[^/@\s]+@')


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
