#!/usr/bin/env python3
"""Move tracked config secrets into ignored runtime env storage and sanitize source."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / 'configs/config.json'
DEFAULT_RUNTIME_PATH = PROJECT_ROOT / 'configs/runtime.json'
PLACEHOLDER_RE = re.compile(r'^\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}$')

KNOWN_SECRET_ENV = {
    ('ubuntu_pswd',): 'GMS_UBUNTU_PASSWORD',
    ('vnc_password',): 'GMS_VNC_PASSWORD',
    ('wifi', 'password'): 'GMS_WIFI_PASSWORD',
    ('gerrit_dashboard', 'rest_password'): 'GMS_GERRIT_REST_PASSWORD',
    ('redmine_dashboard', 'email', 'password'): 'GMS_SMTP_PASSWORD',
    ('redmine_auth', 'encrypted_password'): 'GMS_REDMINE_ENCRYPTED_PASSWORD',
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'{path} must contain a JSON object')
    return payload


def _get_path(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _set_path(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = payload
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _is_literal(value: Any) -> bool:
    if not isinstance(value, str):
        return bool(value)
    stripped = value.strip()
    return bool(stripped) and PLACEHOLDER_RE.fullmatch(stripped) is None


def _provider_env_name(provider_name: str) -> str:
    special = {
        'glm_local': 'GMS_LOCAL_AI_API_KEY',
        'zhipu': 'GMS_ZHIPU_API_KEY',
    }
    if provider_name in special:
        return special[provider_name]
    normalized = re.sub(r'[^A-Za-z0-9]+', '_', provider_name).strip('_').upper()
    return f'GMS_AI_{normalized or "PROVIDER"}_API_KEY'


def _atomic_write_json(path: Path, payload: dict[str, Any], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f'.{path.name}.',
        dir=path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, mode)
        text = json.dumps(payload, ensure_ascii=False, indent=4) + '\n'
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def sanitize_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    runtime_path: Path = DEFAULT_RUNTIME_PATH,
) -> tuple[int, int]:
    config = _read_json(config_path)
    runtime = _read_json(runtime_path)
    migrated = 0
    sanitized = 0

    mappings = dict(KNOWN_SECRET_ENV)
    providers = (
        ((config.get('ai_models') or {}).get('providers') or {})
        if isinstance(config.get('ai_models'), dict)
        else {}
    )
    if isinstance(providers, dict):
        for provider_name, provider in providers.items():
            if isinstance(provider, dict) and 'api_key' in provider:
                mappings[('ai_models', 'providers', str(provider_name), 'api_key')] = (
                    _provider_env_name(str(provider_name))
                )

    for path, env_name in mappings.items():
        current = _get_path(config, path)
        if current is None:
            continue
        if _is_literal(current):
            if not str(runtime.get(env_name) or '').strip():
                runtime[env_name] = str(current)
                migrated += 1
        placeholder = f'${{{env_name}:}}'
        if current != placeholder:
            _set_path(config, path, placeholder)
            sanitized += 1

    _atomic_write_json(runtime_path, runtime, 0o600)
    _atomic_write_json(config_path, config, 0o644)
    return migrated, sanitized


def main() -> int:
    migrated, sanitized = sanitize_config()
    print(
        f'Sanitized {sanitized} tracked secret field(s); '
        f'migrated {migrated} literal value(s) to configs/runtime.json.'
    )
    print('No secret values were printed. configs/runtime.json is mode 0600.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
