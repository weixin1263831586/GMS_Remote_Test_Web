from __future__ import annotations

import asyncio
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar


KeyT = TypeVar('KeyT')
ValueT = TypeVar('ValueT')


class TTLCache(Generic[KeyT, ValueT]):
    """Small thread-safe in-memory cache with monotonic expiry."""

    def __init__(self, ttl_seconds: float, *, max_entries: int | None = None):
        if ttl_seconds < 0:
            raise ValueError('ttl_seconds must be non-negative')
        if max_entries is not None and max_entries < 1:
            raise ValueError('max_entries must be positive')
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: OrderedDict[KeyT, tuple[float, ValueT]] = OrderedDict()
        self._lock = threading.RLock()
        # Fixed stripes bound lock bookkeeping even when keys are unbounded.
        self._load_locks = tuple(threading.Lock() for _ in range(16))
        self._pending: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._generation = 0

    def get(self, key: KeyT, default: ValueT | None = None) -> ValueT | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return default
            created_at, value = item
            if time.monotonic() - created_at >= self.ttl_seconds:
                self._items.pop(key, None)
                return default
            self._items.move_to_end(key)
            return value

    def set(self, key: KeyT, value: ValueT) -> None:
        with self._lock:
            now = time.monotonic()
            expired = [key for key, (created_at, _) in self._items.items()
                       if now - created_at >= self.ttl_seconds]
            for expired_key in expired:
                del self._items[expired_key]
            if self.ttl_seconds == 0:
                return
            self._items[key] = (now, value)
            self._items.move_to_end(key)
            if self.max_entries is not None:
                while len(self._items) > self.max_entries:
                    self._items.popitem(last=False)

    def get_or_load(self, key: KeyT, load: Callable[[], ValueT]) -> ValueT:
        """Coalesce synchronous cold loads without holding the cache-wide lock."""
        cached = self.get(key)
        if cached is not None:
            return cached
        with self._load_locks[hash(key) % len(self._load_locks)]:
            cached = self.get(key)
            if cached is not None:
                return cached
            with self._lock:
                generation = self._generation
            value = load()
            with self._lock:
                if generation == self._generation:
                    self.set(key, value)
            return value

    async def get_or_load_async(self, key: KeyT, load: Callable[[], Awaitable[ValueT]]) -> ValueT:
        """Share a load in the calling event loop; cancellation affects only its waiter."""
        cached = self.get(key)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        with self._lock:
            pending = self._pending.setdefault(loop, {})
            task = pending.get(key)
            if task is None:
                generation = self._generation

                async def fill():
                    value = await load()
                    with self._lock:
                        if generation == self._generation:
                            self.set(key, value)
                    return value

                task = loop.create_task(fill())
                pending[key] = task

                def finished(done):
                    with self._lock:
                        if pending.get(key) is done:
                            pending.pop(key)
                    # Retrieve failures even if every waiter was cancelled.
                    if not done.cancelled():
                        done.exception()

                task.add_done_callback(finished)
        return await asyncio.shield(task)

    def invalidate(self, key: KeyT) -> None:
        with self._lock:
            self._items.pop(key, None)
            self._generation += 1
            for pending in self._pending.values():
                pending.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._generation += 1
            self._pending.clear()
