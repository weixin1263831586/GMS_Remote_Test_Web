from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class SingleFlightTask:
    """Own one asynchronous operation and prevent duplicate concurrent starts."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_result: Any = None
        self.last_error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    async def start(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> asyncio.Task:
        async with self._lock:
            if self.running:
                return self._task
            self.last_result = None
            self.last_error = None
            self._task = asyncio.create_task(self._run(operation))
            return self._task

    async def _run(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        try:
            self.last_result = await operation()
            return self.last_result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.last_error = exc
            raise

    async def cancel(self) -> None:
        async with self._lock:
            task = self._task
            if task is None or task.done():
                self._task = None
                return
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            async with self._lock:
                if self._task is task:
                    self._task = None
