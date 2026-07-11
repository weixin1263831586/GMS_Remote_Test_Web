"""Atomic persistence operations shared by :mod:`foundation.config`."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any


logger = logging.getLogger(__name__)


class ConfigPersistenceMixin:
    """Provide crash-safe static and runtime JSON writes."""

    config_path: str
    runtime_config_path: str
    _runtime_write_lock: threading.RLock

    def save_config(self, config: dict[str, Any]) -> bool:
        return self._write_config_json(self.config_path, config, 'config')

    def save_runtime_config(self, runtime_config: dict[str, Any]) -> bool:
        return self._write_runtime_config_file(dict(runtime_config or {}))

    def save_runtime(self, updates: dict[str, Any]) -> bool:
        return self.update_runtime_config(updates)

    def update_runtime_config(
        self,
        updates: dict[str, Any],
        *,
        remove_keys: set[str] | None = None,
    ) -> bool:
        """Atomically merge selected top-level runtime keys."""
        try:
            with self._runtime_write_lock:
                runtime = self._load_runtime_config() or {}
                runtime.update(dict(updates or {}))
                for key in remove_keys or set():
                    runtime.pop(key, None)
                return self._write_runtime_config_file(
                    runtime,
                    preserve_redmine_auth=False,
                )
        except Exception as exc:
            logger.error('Error updating runtime config: %s', exc)
            return False

    def _write_runtime_config_file(
        self,
        runtime_config: dict[str, Any],
        preserve_redmine_auth: bool = True,
    ) -> bool:
        payload = dict(runtime_config or {})
        with self._runtime_write_lock:
            if preserve_redmine_auth and 'redmine_auth' not in payload:
                existing = self._load_runtime_config()
                if existing and 'redmine_auth' in existing:
                    payload['redmine_auth'] = existing['redmine_auth']
            return self._write_config_json(
                self.runtime_config_path,
                payload,
                'runtime config',
            )

    def _write_config_json(
        self,
        path: str,
        payload: dict[str, Any],
        label: str,
    ) -> bool:
        temporary = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self._runtime_write_lock:
                with open(temporary, 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle, indent=4, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            logger.info('Saved %s to %s', label, path)
            self.invalidate_cache()
            return True
        except Exception as exc:
            logger.error('Error writing %s: %s', label, exc)
            return False
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass
