"""Worker token parsing, persistence, and request authentication.

Tokens live inside ``configs/cluster.json`` under the ``worker_tokens`` key
(a JSON object mapping ``worker_id`` to ``token``).  The cluster config path
can be overridden with the ``GMS_CLUSTER_CONFIG`` environment variable, the
same variable used by :class:`features.cluster.config.ClusterConfig`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi import HTTPException


def _cluster_config_path() -> Path:
    """Return the path to ``configs/cluster.json``."""
    configured = os.getenv("GMS_CLUSTER_CONFIG", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "configs" / "cluster.json"


def _read_cluster_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def worker_tokens(file_path: Path | None = None) -> dict[str, str]:
    """Return the worker→token map stored under ``worker_tokens`` in cluster.json."""
    raw = _read_cluster_raw(file_path or _cluster_config_path())
    tokens = raw.get("worker_tokens")
    if isinstance(tokens, dict):
        return {str(k): str(v) for k, v in tokens.items() if v}
    return {}


def write_worker_tokens(tokens: dict[str, str], file_path: Path | None = None) -> None:
    """Persist the worker→token map into ``worker_tokens`` within cluster.json.

    Existing cluster configuration keys are preserved; only ``worker_tokens``
    is replaced.
    """
    path = file_path or _cluster_config_path()
    raw = _read_cluster_raw(path)
    raw["worker_tokens"] = dict(sorted(tokens.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


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
