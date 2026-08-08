"""Strict SSH host-key verification shared by Paramiko callers."""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import paramiko


_KNOWN_HOSTS_LOCK = threading.Lock()


def known_hosts_path() -> Path:
    configured = os.getenv("GMS_SSH_KNOWN_HOSTS", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".ssh/known_hosts"


def _host_marker(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def scan_ssh_host_keys(host: str, port: int = 22) -> list[dict[str, str]]:
    """Scan untrusted SSH keys for explicit out-of-band administrator review."""
    if not host or not (1 <= int(port) <= 65535):
        raise ValueError("invalid SSH host or port")
    if not all(character.isalnum() or character in ".:_-" for character in host):
        raise ValueError("invalid SSH hostname")
    try:
        completed = subprocess.run(
            ["ssh-keyscan", "-T", "10", "-p", str(port), host],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"SSH host-key scan failed: {exc}") from exc
    keys: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    marker = _host_marker(host, port)
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
        except Exception:
            continue
        if not entry or not entry.key:
            continue
        identity = (entry.key.get_name(), entry.key.get_base64())
        if identity in seen:
            continue
        seen.add(identity)
        keys.append({
            "host": marker,
            "key_type": identity[0],
            "public_key": identity[1],
            "fingerprint": _fingerprint(entry.key),
        })
    if not keys:
        detail = completed.stderr.strip()[:300]
        stdout_preview = completed.stdout.strip()[:300]
        parts = [f"SSH host returned no keys (exit={completed.returncode})"]
        if detail:
            parts.append(detail)
        if stdout_preview:
            parts.append(f"stdout: {stdout_preview}")
        raise RuntimeError(" | ".join(parts))
    return keys


def trust_scanned_ssh_host_keys(
    host: str,
    port: int,
    submitted_keys: list[dict[str, Any]],
    *,
    replace: bool = False,
) -> list[dict[str, str]]:
    """Persist only keys that still match a fresh server-side scan."""
    scanned = scan_ssh_host_keys(host, port)
    submitted = {
        (
            str(item.get("key_type") or ""),
            str(item.get("public_key") or ""),
            str(item.get("fingerprint") or ""),
        )
        for item in submitted_keys or []
        if isinstance(item, dict)
    }
    selected = [
        item for item in scanned
        if (item["key_type"], item["public_key"], item["fingerprint"]) in submitted
    ]
    if not selected or len(selected) != len(submitted):
        raise ValueError("submitted SSH keys no longer match the target host")

    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = _host_marker(host, port)
    with _KNOWN_HOSTS_LOCK:
        existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        retained: list[str] = []
        existing: set[tuple[str, str]] = set()
        conflict = False
        for line in existing_lines:
            try:
                entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
            except Exception:
                entry = None
            if not entry or not entry.key or marker not in entry.hostnames:
                retained.append(line)
                continue
            identity = (entry.key.get_name(), entry.key.get_base64())
            existing.add(identity)
            if identity not in {
                (item["key_type"], item["public_key"]) for item in selected
            }:
                conflict = True
            if not replace:
                retained.append(line)
        if conflict and not replace:
            raise ValueError(
                "SSH host key changed; verify the rotation and retry with replace=true"
            )
        for item in selected:
            identity = (item["key_type"], item["public_key"])
            if replace or identity not in existing:
                retained.append(f"{marker} {item['key_type']} {item['public_key']}")

        descriptor, temporary_raw = tempfile.mkstemp(
            prefix="known-hosts-",
            dir=path.parent,
        )
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, 0o600)
            payload = "\n".join(line for line in retained if line.strip()) + "\n"
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            descriptor = -1
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    return selected


def configure_strict_host_keys(client: paramiko.SSHClient) -> None:
    """Load trusted host keys and reject unknown or changed hosts."""

    client.load_system_host_keys()
    known_hosts = known_hosts_path()
    if known_hosts.is_file():
        client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
