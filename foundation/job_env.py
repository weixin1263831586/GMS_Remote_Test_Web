"""Shared allowlist for job environment variables (R01, 2026-09-08 audit).

A job's ``env`` travels Controller → command payload → Worker subprocess.
Unfiltered keys such as ``BASH_ENV`` / ``ENV`` / ``SHELLOPTS`` change how the
Worker's Bash wrapper starts and let a restricted test account escalate to the
Worker OS account's execution ability. Both ends therefore filter with the
same allowlist: the Controller rejects unknown keys before the command is
queued, and the Worker re-filters on receipt so a poisoned or replayed
payload can never inject extra startup behaviour.

Only keys with a real consumer are listed:

- ``GMS_TRANSPORT_REQUIREMENT`` — features/devices/transport_policy.py
  transport classification.
- ``GMS_COPY_ROUTE_NETWORK`` / ``GMS_COPY_ROUTE_GATEWAY`` —
  scripts/run_GMS_Test_Auto.sh report-copy return route.
- ``GMS_LOCAL_SERVER`` / ``LOCAL_SERVER`` — deployment-provided local server
  hint consumed by the test workflow.
- ``TERM`` — keeps Tradefed / pytest output sane inside the Bash wrapper.
"""

from __future__ import annotations

import re
from typing import Any

ALLOWED_JOB_ENV_KEYS: frozenset[str] = frozenset({
    "GMS_TRANSPORT_REQUIREMENT",
    "GMS_COPY_ROUTE_NETWORK",
    "GMS_COPY_ROUTE_GATEWAY",
    "GMS_LOCAL_SERVER",
    "LOCAL_SERVER",
    "TERM",
})

# 值不允许换行 / NUL：环境变量值中的换行会把启动包装变成多行脚本。
_JOB_ENV_VALUE_MAX_LEN = 4096
_JOB_ENV_VALUE_RE = re.compile(r"^[^\r\n\x00]*$")


def filter_job_env(
    env: dict[str, Any] | None,
) -> tuple[dict[str, str], list[str]]:
    """Return ``(allowed, rejected_keys)`` for a job env mapping.

    ``allowed`` keeps only allowlisted keys with newline/NUL-free values that
    fit the length bound; everything else is reported in ``rejected_keys`` so
    callers can fail the request (Controller) or log and drop (Worker).
    """

    allowed: dict[str, str] = {}
    rejected: list[str] = []
    for raw_key, raw_value in (env or {}).items():
        key = str(raw_key)
        value = str(raw_value)
        if (
            key in ALLOWED_JOB_ENV_KEYS
            and len(value) <= _JOB_ENV_VALUE_MAX_LEN
            and _JOB_ENV_VALUE_RE.match(value)
        ):
            allowed[key] = value
        else:
            rejected.append(key)
    return allowed, rejected
