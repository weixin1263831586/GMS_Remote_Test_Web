"""Register copied suite artifacts without hiding task-pool backpressure."""

from __future__ import annotations

import contextlib
import os

from foundation.error_model import ApiError
from foundation.errors import TaskCapacityError

from . import runtime


def register_suite_apk_task(task_id: str, apk_path: str, filename: str, owner_id: str) -> None:
    try:
        runtime.create_apk_task(task_id, apk_path, filename, owner_id)
    except TaskCapacityError as exc:
        # Only this rejected request's copy is disposable; existing tasks and
        # other owners' durable results must stay intact.
        runtime.cleanup_files([apk_path])
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(apk_path))
        raise ApiError.dependency_unavailable(
            "当前账号的 APK 分析任务容量已满，请稍后重试，或删除不再需要的分析任务",
            next_actions=({"action": "在 APK 分析页面检查并删除当前账号不再需要的任务；无可清理任务时联系管理员"},),
        ) from exc
