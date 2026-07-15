"""Worker token parsing, persistence, and request authentication."""

from __future__ import annotations

import hashlib
import hmac
import os
import shlex
from pathlib import Path

from fastapi import HTTPException


def worker_tokens() -> dict[str, str]:
    result = {}
    for item in os.getenv("GMS_CLUSTER_WORKER_TOKENS", "").split(","):
        worker_id, separator, token = item.partition(":")
        if separator and worker_id.strip() and token.strip():
            result[worker_id.strip()] = token.strip()
    return result


def write_worker_tokens(tokens: dict[str, str], env_path: Path | None = None) -> None:
    value = ",".join(f"{key}:{item}" for key, item in sorted(tokens.items()))
    os.environ["GMS_CLUSTER_WORKER_TOKENS"] = value
    env_path = env_path or (Path(__file__).resolve().parents[2] / ".env.production")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    # restart_services.sh sources this file, so the value must be shell-safe.
    replacement = f"GMS_CLUSTER_WORKER_TOKENS={shlex.quote(value)}"
    lines = [
        replacement if line.startswith("GMS_CLUSTER_WORKER_TOKENS=") else line
        for line in lines
    ]
    if not any(line.startswith("GMS_CLUSTER_WORKER_TOKENS=") for line in lines):
        lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)


def persist_worker_token(worker_id: str, token: str) -> None:
    tokens = worker_tokens()
    tokens[worker_id] = token
    write_worker_tokens(tokens)


def authenticate_worker(worker_id: str, authorization: str | None) -> None:
    expected = worker_tokens().get(worker_id)
    if not expected:
        raise HTTPException(503, f"worker token is not configured for {worker_id}")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    ):
        raise HTTPException(401, "invalid worker token")
