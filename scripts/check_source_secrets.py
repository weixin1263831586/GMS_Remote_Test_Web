#!/usr/bin/env python3
"""Reject literal secrets in tracked files without printing values.

Scans JSON configuration files for sensitive keys with literal values, and
every other tracked text file for well-known credential content markers
(private-key blocks, Google service-account fields, API key prefixes).
"""

from __future__ import annotations

import json
import re
import subprocess
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

# Content markers that identify credential material regardless of file name.
# The PEM pattern is anchored on a long base64 body so bare validation
# snippets ("---BEGIN...---" containment checks) do not trip it.
PRIVATE_KEY_BLOCK = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----\s+[A-Za-z0-9+/=]{64,}'
)
CONTENT_MARKERS = {
    'private_key_id': re.compile(r'"private_key_id"\s*:'),
    'client_secret': re.compile(r'"client_secret"\s*:'),
    'google_api_key': re.compile(r'AIza[0-9A-Za-z_-]{30,}'),
    'github_token': re.compile(r'gh[posr]_[A-Za-z0-9]{30,}'),
    'openai_key': re.compile(r'sk-[A-Za-z0-9]{20,}'),
}

# Files whose names alone mark credential material.
DENIED_BASENAMES = {
    'gts-rockchip.json',
}

# The scanner itself necessarily embeds the detection patterns. Test files are
# deliberately scanned: credentials copied into fixtures are still leaked.
_SELF_BASENAME = Path(__file__).name


def _is_scan_exempt(path: Path) -> bool:
    return path.resolve() == Path(__file__).resolve()


def _is_structured_json_exempt(path: Path, root: Path) -> bool:
    """OpenAPI/config-shape snapshots describe fields; they do not store values."""
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    return relative in {
        'tests/contract/snapshots/config_shape.json',
        'tests/contract/snapshots/openapi.json',
    }


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


def _tracked_files(root: Path) -> list[Path]:
    try:
        listing = subprocess.run(
            ['git', '-C', str(root), 'ls-files'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted(
        path for name in listing
        if name and (path := root / name).is_file()
    )


def check_content_markers(path: Path) -> list[str]:
    """Scan an arbitrary text file for credential-shaped content."""
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return []
    findings: list[str] = []
    if PRIVATE_KEY_BLOCK.search(text):
        findings.append(f'{path}: PEM private key block')
    for marker, pattern in CONTENT_MARKERS.items():
        if pattern.search(text):
            findings.append(f'{path}: credential content marker ({marker})')
    return findings


def scan_tracked_files(root: Path) -> list[str]:
    """Return secret findings for every tracked text file beneath ``root``."""
    findings: list[str] = []
    for path in _tracked_files(root):
        if _is_scan_exempt(path):
            continue
        if path.name in DENIED_BASENAMES:
            findings.append(f'{path}: denied credential filename')
            continue
        if path.suffix == '.json' and not _is_structured_json_exempt(path, root):
            findings.extend(check_json_file(path))
        try:
            text_bytes = path.read_bytes()
        except OSError:
            continue
        if b'\x00' in text_bytes[:8192]:
            continue  # binary
        findings.extend(check_content_markers(path))
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    files = _tracked_files(root)
    if not files:
        print('No tracked files found; skipping secret scan.', file=sys.stderr)
        return 0

    findings = scan_tracked_files(root)

    if findings:
        print('Tracked files contain literal secret material:', file=sys.stderr)
        for finding in findings:
            print(f'  - {finding}', file=sys.stderr)
        print(
            'Move credentials into configs/runtime.json environment entries '
            '(or a real environment); never commit them.',
            file=sys.stderr,
        )
        return 1

    print('Tracked-file secret check passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
