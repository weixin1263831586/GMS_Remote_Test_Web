from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


class DuplicateExecutionError(RuntimeError):
    pass


class TestExecutionService:
    def __init__(
        self,
        *,
        release_devices: Callable[[str, list[str]], Any] | None = None,
    ):
        self._states: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._release_devices = release_devices

    def _state(self, client_id: str) -> dict[str, Any]:
        return self._states.setdefault(
            client_id,
            {
                "running": False,
                "devices": [],
                "logs": [],
                "cancelled": False,
            },
        )

    async def start(
        self,
        client_id: str,
        devices: list[str],
        *,
        on_started: Callable[[], Awaitable[Any] | Any] | None = None,
    ) -> dict[str, Any]:
        state = self._state(client_id)
        if state["running"]:
            raise DuplicateExecutionError(
                f"Execution already active for {client_id}"
            )

        state.update(
            {
                "running": True,
                "devices": list(devices),
                "logs": [],
                "cancelled": False,
            }
        )
        try:
            if on_started is not None:
                result = on_started()
                if inspect.isawaitable(result):
                    await result
        except Exception:
            await self._release(client_id, state["devices"])
            state.update(
                {
                    "running": False,
                    "devices": [],
                    "cancelled": True,
                }
            )
            raise
        return self.status(client_id)

    async def stop(self, client_id: str) -> dict[str, Any]:
        state = self._state(client_id)
        was_running = bool(state["running"])
        devices = list(state["devices"])
        state.update(
            {
                "running": False,
                "devices": [],
                "cancelled": True,
            }
        )
        if devices:
            await self._release(client_id, devices)
        return {**self.status(client_id), "was_running": was_running}

    async def clean(self, client_id: str) -> dict[str, Any]:
        await self.stop(client_id)
        state = self._state(client_id)
        state["logs"] = []
        return self.status(client_id)

    def append_log(self, client_id: str, message: str) -> None:
        self._state(client_id)["logs"].append(message)

    def status(self, client_id: str) -> dict[str, Any]:
        state = self._state(client_id)
        return {
            "running": bool(state["running"]),
            "devices": list(state["devices"]),
            "logs": list(state["logs"]),
            "cancelled": bool(state["cancelled"]),
        }

    async def stream(
        self,
        client_id: str,
        *,
        poll_interval: float = 0.1,
    ) -> AsyncIterator[str]:
        index = 0
        while True:
            state = self._state(client_id)
            logs = state["logs"]
            while index < len(logs):
                yield logs[index]
                index += 1
            if not state["running"]:
                return
            await asyncio.sleep(poll_interval)

    def create_task(self, kind: str) -> str:
        task_id = uuid.uuid4().hex
        self._tasks[task_id] = {
            "task_id": task_id,
            "kind": kind,
            "status": "pending",
            "progress": 0,
            "created_at": time.time(),
        }
        return task_id

    def update_task(self, task_id: str, **updates: Any) -> dict[str, Any]:
        task = self._tasks[task_id]
        task.update(updates)
        return dict(task)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        return dict(task) if task is not None else None

    async def _release(
        self,
        client_id: str,
        devices: list[str],
    ) -> None:
        if self._release_devices is None:
            return
        result = self._release_devices(client_id, devices)
        if inspect.isawaitable(result):
            await result
