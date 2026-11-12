"""Shared GMS_LOG_LEVEL parsing for Controller and Worker processes."""

from __future__ import annotations

import logging
import os
import sys


_ALIASES = {
    'WARN': 'WARNING',
    'FATAL': 'CRITICAL',
}


def resolve_log_level(raw: str | None, *, default: int = logging.INFO) -> int:
    """Map ``GMS_LOG_LEVEL`` to a logging level.

    Accepts standard level names, the ``WARN`` / ``FATAL`` aliases, and a
    non-negative integer. Blank input uses ``default``. An unknown value
    falls back to INFO and writes one warning to stderr.
    """

    text = str(raw or '').strip().upper()
    if not text:
        return default
    level = getattr(logging, _ALIASES.get(text, text), None)
    if isinstance(level, int):
        return level
    try:
        numeric = int(text)
    except ValueError:
        numeric = -1
    if numeric >= 0:
        return numeric
    print(f'Unknown GMS_LOG_LEVEL {raw!r}; using INFO', file=sys.stderr)
    return default


def configure_stderr_logging(raw: str | None = None) -> int:
    """Apply ``GMS_LOG_LEVEL`` even when the root logger already has a handler.

    ``basicConfig`` ignores ``level`` once any handler exists (uvicorn imports
    the app that way). Setting the root level afterwards keeps the flag
    effective without replacing handlers the server already installed.
    """

    if raw is None:
        raw = os.environ.get('GMS_LOG_LEVEL', 'INFO')
    level = resolve_log_level(raw)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    logging.getLogger().setLevel(level)
    return level
