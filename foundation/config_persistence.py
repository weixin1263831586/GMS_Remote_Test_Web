"""Atomic persistence operations shared by :mod:`foundation.config`."""

from __future__ import annotations

import logging
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from foundation.private_config import write_private_json


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
            with self._runtime_write_lock, self._runtime_transaction():
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
        with self._runtime_write_lock, self._runtime_transaction():
            if preserve_redmine_auth and 'redmine_auth' not in payload:
                existing = self._load_runtime_config()
                if existing and 'redmine_auth' in existing:
                    payload['redmine_auth'] = existing['redmine_auth']
            if self._uses_runtime_store():
                try:
                    self._runtime_store.write(payload)
                    self.invalidate_cache()
                    return True
                except Exception as exc:
                    logger.error('Error writing partitioned runtime config: %s', type(exc).__name__)
                    return False
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
        try:
            with self._runtime_write_lock:
                write_private_json(Path(path), payload)
            logger.info('Saved %s to %s', label, path)
            self.invalidate_cache()
            return True
        except Exception as exc:
            logger.error('Error writing %s: %s', label, exc)
            return False

    def _runtime_transaction(self):
        return self._runtime_store.locked() if self._uses_runtime_store() else nullcontext()
