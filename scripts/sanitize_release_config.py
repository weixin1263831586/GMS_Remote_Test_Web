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
            "product_branding": {
                "company_name": "Organization",
                "company_url": "",
                "company_icon": "🏢",
                "company_keywords": [],
                "tool_icon_overrides": {},
                "browser_icon_candidates": {},
            },
        }
    )
    for section_name in ("external_services", "opengrok", "redmine"):
        section = result.get(section_name)
        if isinstance(section, dict):
            result[section_name] = {
                section_key: _empty_like(section_value)
                for section_key, section_value in section.items()
            }
    redmine_auth = result.get("redmine_auth")
    if isinstance(redmine_auth, dict):
        redmine_auth.update({key: "" for key in redmine_auth})
    gerrit = result.get("gerrit_dashboard")
    if isinstance(gerrit, dict):
        for key in (
            "base_url",
            "rest_username",
            "ssh_host",
            "ssh_user",
            "ssh_identity_file",
            "default_owner",
        ):
            if key in gerrit:
                gerrit[key] = ""
        for key in (
            "department_defaults",
            "dashboard_profiles",
            "personal_profiles",
            "department_profiles",
        ):
            if key in gerrit:
                gerrit[key] = _empty_like(gerrit[key])
    ai_models = result.get("ai_models")
    if isinstance(ai_models, dict) and isinstance(ai_models.get("providers"), dict):
        for provider in ai_models["providers"].values():
            if isinstance(provider, dict):
                for key in ("base_url", "endpoint"):
                    if key in provider:
                        provider[key] = ""
    return result


def sanitize_cluster_config(config: dict[str, Any]) -> dict[str, Any]:
    """Keep capacity defaults while removing the source deployment identity."""
    result = sanitize_value(config)
    result.update(
        {
            "enabled": False,
            "controller_url": "",
            "local_worker_id": "ats-worker-controller",
            "remote_dispatch_enabled": False,
            "global_device_pool_enabled": False,
            "lease_enforcement_enabled": False,
        }
    )
    return result


def sanitize_named_config(path: Path, raw: Any) -> Any:
    if path.name == "config.json" and path.parent.name == "configs":
        return sanitize_product_config(raw if isinstance(raw, dict) else {})
    if path.name == "automation_profiles.json":
        return {"profiles": []}
    if path.name == "build_servers.json":
        return {"servers": [], "templates": []}
    if path.name == "cluster.json":
        return sanitize_cluster_config(raw if isinstance(raw, dict) else {})
    return sanitize_value(raw)


def sanitize_file(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    sanitized = sanitize_named_config(path, raw)
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
