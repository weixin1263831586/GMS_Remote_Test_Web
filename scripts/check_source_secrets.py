#!/usr/bin/env python3
"""Reject literal secrets in tracked JSON configuration without printing values."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    'api_key',
    'authorization',
    'encrypted_password',
    'password',
    'private_key',
    'rest_password',
    'secret',
    'token',
    'ubuntu_pswd',
    'vnc_password',
}
SENSITIVE_SUFFIXES = ('_api_key', '_password', '_pswd', '_secret', '_token')
SAFE_PLACEHOLDER = re.compile(
    r'^\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}$'
)


def _is_literal_secret(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped) and SAFE_PLACEHOLDER.fullmatch(stripped) is None
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def find_literal_secret_paths(value: Any, prefix: str = '') -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            path = f'{prefix}.{key}' if prefix else key
            normalized = key.strip().lower()
            if (
                normalized in SENSITIVE_KEYS
                or normalized.endswith(SENSITIVE_SUFFIXES)
            ) and _is_literal_secret(item):
                findings.append(path)
            findings.extend(find_literal_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(
                find_literal_secret_paths(item, f'{prefix}[{index}]')
            )
    return findings


def check_json_file(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f'{path}: unable to parse JSON ({exc})']
    return [f'{path}:{item}' for item in find_literal_secret_paths(payload)]


def main(argv: list[str]) -> int:
    targets = [Path(item) for item in argv[1:]] or [Path('configs/config.json')]
    findings: list[str] = []
    for path in targets:
        if not path.is_file():
            findings.append(f'{path}: file not found')
            continue
        findings.extend(check_json_file(path))

    if findings:
        print('Tracked configuration contains literal secret values:', file=sys.stderr)
        for finding in findings:
            print(f'  - {finding}', file=sys.stderr)
        print(
            'Run: python3 scripts/sanitize_tracked_config.py',
            file=sys.stderr,
        )
        return 1

    print('Tracked configuration secret check passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
