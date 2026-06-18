from __future__ import annotations

import threading
import time
from typing import Generic, TypeVar


KeyT = TypeVar('KeyT')
ValueT = TypeVar('ValueT')


class TTLCache(Generic[KeyT, ValueT]):
    """Small thread-safe in-memory cache with monotonic expiry."""

    def __init__(self, ttl_seconds: float):
        if ttl_seconds < 0:
            raise ValueError('ttl_seconds must be non-negative')
        self.ttl_seconds = ttl_seconds
        self._items: dict[KeyT, tuple[float, ValueT]] = {}
        self._lock = threading.RLock()

    def get(self, key: KeyT, default: ValueT | None = None) -> ValueT | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return default
            created_at, value = item
            if time.monotonic() - created_at >= self.ttl_seconds:
                self._items.pop(key, None)
                return default
            return value

    def set(self, key: KeyT, value: ValueT) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), value)

    def invalidate(self, key: KeyT) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
