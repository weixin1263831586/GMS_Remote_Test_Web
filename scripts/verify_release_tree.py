#!/usr/bin/env python3
"""Fail release packaging when runtime data or credentials enter the tree."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


DENIED_NAMES = {
    ".editorconfig",
    ".gitignore",
    ".env.production",
    "2.txt",
    "AGENTS.md",
    "conftest.py",
    "env.production",
    "pyproject.toml",
    "runtime.json",
    "config_runtime.json",
    "client_ssh_credentials.local.json",
    "redmine_auth.json",
    "redmine_user_map.json",
    "user_tools_data.json",
    "worker_tokens.json",
    "gts-rockchip.json",
    "platform-tools-gms-linux.zip",
}
DENIED_COMPONENTS = {
    ".agents",
    ".codex",
    ".git",
    ".certs",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "certs",
    "scripts_local",
    "superpowers",
    "tests",
    "jdk-11",
}
DENIED_SUFFIXES = {".map"}
INTERNAL_DOCUMENTS = {
    "docs/android-cli-ui-control-integration.md",
    "docs/build-server-integration-assessment.md",
    "docs/code-audit-2026-07.md",
    "docs/code-audit-2026-08-12.md",
    "docs/multi-host-cluster-implementation-plan.md",
    "docs/product-integration-cluster-audit-2026-07-15.md",
    "docs/product-release-checklist-2026-07-15.md",
    "docs/refactor-baseline.md",
    "docs/refactor-parity-audit.md",
    "docs/refactor-verification.md",
    "docs/wiki-knowledge-base-plan.md",
}
SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "encrypted_password",
    "password",
    "private_key",
    "rest_password",
    "secret",
    "token",
    "ubuntu_pswd",
    "vnc_password",
}
SENSITIVE_SUFFIXES = ("_api_key", "_password", "_pswd", "_secret", "_token")
PRIVATE_KEY_BLOCK = re.compile(
    rb"-----BEGIN (?P<kind>(?:OPENSSH |RSA |EC )?PRIVATE KEY)-----[\r\n]+"
    rb"[A-Za-z0-9+/=\r\n]{80,}"
    rb"-----END (?P=kind)-----"
)


def _is_nonempty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _json_secret_paths(value: Any, prefix: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            normalized = key.strip().lower()
            if (
                normalized in SENSITIVE_KEYS
                or normalized.endswith(SENSITIVE_SUFFIXES)
            ) and _is_nonempty(item):
                findings.append(path)
            findings.extend(_json_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_json_secret_paths(item, f"{prefix}[{index}]"))
    return findings


def verify_release_tree(root: Path) -> list[str]:
    root = root.resolve()
    findings: list[str] = []
    if not root.is_dir():
        return [f"release root is not a directory: {root}"]
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        relative_text = relative.as_posix()
        if any(part in DENIED_COMPONENTS for part in relative.parts):
            findings.append(f"denied path: {relative}")
            continue
        if path.name in DENIED_NAMES:
            findings.append(f"runtime file: {relative}")
        if relative_text in INTERNAL_DOCUMENTS:
            findings.append(f"internal document: {relative}")
        if path.suffix.lower() in DENIED_SUFFIXES:
            findings.append(f"source map: {relative}")
        if path.is_symlink():
            target = (path.parent / os.readlink(path)).resolve()
            if target != root and root not in target.parents:
                findings.append(f"external symlink: {relative}")
            continue
        if not path.is_file():
            continue
        try:
            prefix = path.read_bytes()[:8192]
        except OSError as exc:
            findings.append(f"unreadable file: {relative}: {exc}")
            continue
        normalized_prefix = prefix.replace(b"\\r\\n", b"\n").replace(
            b"\\n", b"\n"
        )
        if PRIVATE_KEY_BLOCK.search(normalized_prefix):
            findings.append(f"private key material: {relative}")
        if path.suffix.lower() == ".json" and path.stat().st_size <= 10 * 1024 * 1024:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            for secret_path in _json_secret_paths(payload):
                findings.append(f"non-empty secret {relative}:{secret_path}")
    return sorted(set(findings))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: verify_release_tree.py <release-root>", file=sys.stderr)
        return 2
    findings = verify_release_tree(Path(argv[1]))
    if findings:
        for finding in findings:
            print(f"release verification failed: {finding}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
