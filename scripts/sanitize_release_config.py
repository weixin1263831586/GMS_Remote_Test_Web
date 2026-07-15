#!/usr/bin/env python3
"""Remove machine credentials and runtime identity from packaged JSON config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "api_key",
    "encrypted_password",
    "password",
    "rest_password",
    "secret",
    "token",
    "ubuntu_pswd",
    "vnc_password",
}
SENSITIVE_SUFFIXES = ("_api_key", "_password", "_pswd", "_secret", "_token")


def _empty_like(value: Any) -> Any:
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    return ""


def sanitize_value(value: Any, key: str = "") -> Any:
    normalized = key.strip().lower()
    if normalized in SENSITIVE_KEYS or normalized.endswith(SENSITIVE_SUFFIXES):
        return _empty_like(value)
    if isinstance(value, dict):
        return {item_key: sanitize_value(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    return value


def sanitize_product_config(config: dict[str, Any]) -> dict[str, Any]:
    result = sanitize_value(config)
    result.update(
        {
            "ubuntu_user": "",
            "ubuntu_host": "127.0.0.1",
            "local_server": "",
            "private_key_path": "",
            "client_hosts": {},
            "client_ssh_credentials": [],
            "device_groups": [],
        }
    )
    redmine_auth = result.get("redmine_auth")
    if isinstance(redmine_auth, dict):
        redmine_auth.update({key: "" for key in redmine_auth})
    gerrit = result.get("gerrit_dashboard")
    if isinstance(gerrit, dict):
        for key in ("rest_username", "ssh_user", "ssh_identity_file", "default_owner"):
            if key in gerrit:
                gerrit[key] = ""
    return result


def sanitize_file(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    sanitized = sanitize_product_config(raw) if path.name == "config.json" and path.parent.name == "configs" else sanitize_value(raw)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: sanitize_release_config.py <config.json> [...]", file=sys.stderr)
        return 2
    for value in argv[1:]:
        path = Path(value)
        if path.is_file():
            sanitize_file(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
