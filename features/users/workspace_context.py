"""Per-user navigation and execution context.

The cluster feature flag describes Controller infrastructure.  The selected
Worker, devices, suite and active run are user workspace state and must not be
stored in a process-global toggle.  This module persists that small state under
``data/user_prefs`` so page changes, reloads and embedded feature pages share a
single source of truth.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from foundation.responses import success_response

from . import runtime
from .clients import owner_id_from_request
from .storage_paths import owner_storage_key


router = APIRouter(prefix="/api/users/workspace-context")
_storage_lock = threading.RLock()
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:@/+-]*$")


def _is_tcp_serial(value: str) -> bool:
    """Structural adb TCP-serial detection, not prefix guessing.

    A TCP serial is exactly ``host:port`` where host is an IPv4 literal, an
    IPv6 literal (possibly bracketed), or a hostname without colons, and
    port is 1-5 digits. Multiple colons only parse as ``[v6]:port`` —
    ``2001:db8::1:5555`` (bare IPv6 with port) is NOT a valid adb serial
    form and must not be kept as a device id; conversely
    ``remote:12345`` IS a valid TCP serial (adb accepts hostnames) even
    though the host part is not an IP.
    """
    text = str(value or "").strip()
    if not text or ":" not in text:
        return False
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit():
        return False
    if not 1 <= len(port) <= 5:
        return False
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        try:
            ipaddress.IPv6Address(host)
            return True
        except ValueError:
            return False
    if ":" in host:
        # Bare IPv6 with :port is ambiguous; adb requires brackets.
        return False
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        # Hostname form: adb serials allow [A-Za-z0-9._-] host labels.
        return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*)", host))


def _local_worker_id() -> str:
    from foundation.cluster_port import get_local_worker_id

    return get_local_worker_id()


class WorkspaceContextPatch(BaseModel):
    scope_mode: Literal["single", "cluster"] | None = None
    worker_id: str | None = Field(default=None, max_length=128)
    device_ids: list[str] | None = Field(default=None, max_length=32)
    suite_key: str | None = Field(default=None, max_length=512)
    suite_path: str | None = Field(default=None, max_length=4096)
    cluster_job_id: str | None = Field(default=None, max_length=128)
    attempt_id: str | None = Field(default=None, max_length=128)
    automation_run_id: str | None = Field(default=None, max_length=128)
    report_id: str | None = Field(default=None, max_length=256)
    report_timestamp: str | None = Field(default=None, max_length=256)
    artifact_id: str | None = Field(default=None, max_length=128)
    gerrit_change_id: str | None = Field(default=None, max_length=256)
    gerrit_patchset: str | None = Field(default=None, max_length=64)
    redmine_issue_id: str | None = Field(default=None, max_length=64)
    origin_page: str | None = Field(default=None, max_length=64)

    @field_validator("worker_id", "cluster_job_id", "attempt_id", "automation_run_id", "artifact_id")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if value and not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("invalid workspace identifier")
        return value

    @field_validator("device_ids")
    @classmethod
    def normalize_device_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = []
        for value in values:
            item = str(value or "").strip()
            if not item or len(item) > 384 or not _IDENTIFIER_RE.fullmatch(item):
                continue
            if item not in normalized:
                normalized.append(item)
        return normalized


def _default_context() -> dict:
    return {
        "scope_mode": "single",
        "worker_id": _local_worker_id(),
        "device_ids": [],
        "suite_key": "",
        "suite_path": "",
        "cluster_job_id": "",
        "attempt_id": "",
        "automation_run_id": "",
        "report_id": "",
        "report_timestamp": "",
        "artifact_id": "",
        "gerrit_change_id": "",
        "gerrit_patchset": "",
        "redmine_issue_id": "",
        "origin_page": "test",
        "updated_at": "",
    }


def _context_path(owner_id: str) -> Path:
    root = Path(runtime.data_root) / "user_prefs" / owner_storage_key(owner_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / "workspace_context.json"


def load_workspace_context(owner_id: str) -> dict:
    context = _default_context()
    path = _context_path(owner_id)
    with _storage_lock:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    if isinstance(raw, dict):
        for key in context:
            if key in raw:
                context[key] = raw[key]
    # Single-host scope is always anchored to the local Worker.
    if context.get("scope_mode") != "cluster":
        context["scope_mode"] = "single"
        context["worker_id"] = _local_worker_id()
    return context


def save_workspace_context(owner_id: str, patch: WorkspaceContextPatch) -> dict:
    path = _context_path(owner_id)
    with _storage_lock:
        current = load_workspace_context(owner_id)
        updates = patch.model_dump(exclude_unset=True)
        for key, value in updates.items():
            current[key] = "" if value is None and key != "device_ids" else ([] if value is None else value)
        if current.get("scope_mode") != "cluster":
            local_worker_id = _local_worker_id()
            current["scope_mode"] = "single"
            current["worker_id"] = local_worker_id
            current["device_ids"] = [
                value for value in current.get("device_ids", [])
                # a colon means either a worker namespace prefix
                # ("worker:serial") or a TCP serial ("10.0.0.5:5555").
                # Strip only foreign worker prefixes; TCP serials without
                # the local worker prefix must survive the single-mode save.
                if ":" not in value
                or value.startswith(f"{local_worker_id}:")
                or _is_tcp_serial(value)
            ]
        elif not current.get("worker_id"):
            current["worker_id"] = _local_worker_id()
        current["updated_at"] = datetime.now(timezone.utc).isoformat()

        fd, temporary = tempfile.mkstemp(prefix="workspace-context-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return current


@router.get("")
async def get_workspace_context(request: Request):
    owner_id = owner_id_from_request(request)
    return success_response({"context": load_workspace_context(owner_id), "owner_id": owner_id})


@router.patch("")
async def patch_workspace_context(request: Request, patch: WorkspaceContextPatch):
    owner_id = owner_id_from_request(request)
    context = save_workspace_context(owner_id, patch)
    return success_response({"context": context, "owner_id": owner_id})


@router.delete("")
async def reset_workspace_context(request: Request):
    owner_id = owner_id_from_request(request)
    path = _context_path(owner_id)
    with _storage_lock:
        path.unlink(missing_ok=True)
    return success_response({"context": _default_context(), "owner_id": owner_id})


__all__ = [
    "WorkspaceContextPatch",
    "load_workspace_context",
    "save_workspace_context",
]
