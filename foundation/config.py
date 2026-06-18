from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except (AttributeError, TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RuntimeSettings:
    project_root: Path
    data_root: Path
    server_host: str
    server_port: int
    environment: str
    proxy_headers_enabled: bool
    forwarded_allow_ips: str

    @classmethod
    def from_environment(
        cls,
        *,
        project_root: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> RuntimeSettings:
        env = os.environ if environ is None else environ
        root = (
            Path(__file__).resolve().parents[1]
            if project_root is None
            else Path(project_root).resolve()
        )
        environment = env.get('GMS_ENV', 'development').strip().lower()
        return cls(
            project_root=root,
            data_root=Path(env.get('GMS_DATA_ROOT', str(root / 'data'))).resolve(),
            server_host=env.get(
                'GMS_SERVER_HOST',
                '127.0.0.1' if environment == 'production' else '0.0.0.0',
            ),
            server_port=_env_int(env.get('GMS_PORT', '5001'), 5001),
            environment=environment,
            proxy_headers_enabled=_env_bool(
                env.get(
                    'GMS_PROXY_HEADERS',
                    'true' if environment == 'production' else 'false',
                )
            ),
            forwarded_allow_ips=env.get(
                'GMS_FORWARDED_ALLOW_IPS',
                '127.0.0.1',
            ),
        )


settings = RuntimeSettings.from_environment()


class ConfigManager:
    """Read and atomically update the existing static/runtime configuration."""

    def __init__(self, project_root: Path | str | None = None, cache_ttl: float = 5):
        self.project_root = (
            settings.project_root
            if project_root is None
            else Path(project_root).resolve()
        )
        self.config_path = self.project_root / 'configs/config.json'
        self.runtime_config_path = self.project_root / 'configs/config_runtime.json'
        self._cache_ttl = cache_ttl
        self._cache: dict[str, Any] | None = None
        self._cache_timestamp = 0.0
        self._static_mtime = 0.0
        self._runtime_mtime = 0.0
        self._lock = threading.RLock()

    def load_config(self, force_reload: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if not force_reload and self._cache_valid(now):
                return copy.deepcopy(self._cache)
            static = self._read_json(self.config_path)
            runtime = self._read_json(self.runtime_config_path)
            merged = dict(static)
            static_ai = merged.get('ai_models')
            merged.update(runtime)
            if static_ai:
                merged['ai_models'] = static_ai
            self._cache = merged
            self._cache_timestamp = now
            self._static_mtime = self._mtime(self.config_path)
            self._runtime_mtime = self._mtime(self.runtime_config_path)
            return copy.deepcopy(merged)

    def save_runtime(self, updates: Mapping[str, Any]) -> bool:
        with self._lock:
            runtime = self._read_json(self.runtime_config_path)
            runtime.update(copy.deepcopy(dict(updates)))
            self._atomic_write_json(self.runtime_config_path, runtime)
            self.invalidate_cache()
            return True

    def invalidate_cache(self) -> None:
        self._cache = None
        self._cache_timestamp = 0.0

    def _cache_valid(self, now: float) -> bool:
        return (
            self._cache is not None
            and now - self._cache_timestamp <= self._cache_ttl
            and self._mtime(self.config_path) == self._static_mtime
            and self._mtime(self.runtime_config_path) == self._runtime_mtime
        )

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f'configuration root must be an object: {path}')
        return value

    @staticmethod
    def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f'.{path.name}.',
            suffix='.tmp',
        )
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temp_name)
            raise
