from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any


class FirmwareTaskService:
    def __init__(self):
        self._tasks: dict[str, dict[str, Any]] = {}

    async def run(
        self,
        task_id: str,
        operation,
        *,
        cleanup_paths: list[Path] | None = None,
    ) -> Any:
        self._tasks[task_id] = {"status": "running", "error": None}
        try:
            result = operation()
            if inspect.isawaitable(result):
                result = await result
            self._tasks[task_id] = {"status": "completed", "error": None}
            return result
        except Exception as exc:
            self._tasks[task_id] = {
                "status": "error",
                "error": str(exc),
            }
            for path in cleanup_paths or []:
                path.unlink(missing_ok=True)
            raise

    def status(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        return dict(task) if task else None
