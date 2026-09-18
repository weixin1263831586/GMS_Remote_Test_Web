"""Cross-process cancellation helpers for Daily Brief KkAgent work."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from .daily_brief_analysis_events import finish_analysis_progress
from .users import _now


CANCEL_POLL_SECONDS = 0.25
logger = logging.getLogger("daily_brief_worker")


class RunCancelledError(Exception):
    """A persisted run cancellation was observed by an executing Worker."""


async def analyze_with_persisted_cancel(
    repository: Any,
    run_id: str,
    analyzer: Any,
    entry: dict[str, Any],
) -> Any:
    """Run one analysis while polling the cross-process cancellation flag.

    ``entry["_progress_recorder"]`` 存在时，本包装器同时负责进度时间线的
    终态事件（完成 / 已停止）：终态收敛只有这一处，调用方无需重复落库。
    """
    progress = entry.get("_progress_recorder") if isinstance(entry, dict) else None
    analysis_task = asyncio.create_task(analyzer.analyze(entry))
    try:
        while True:
            done, _pending = await asyncio.wait(
                {analysis_task}, timeout=CANCEL_POLL_SECONDS
            )
            if analysis_task in done:
                outcome = await analysis_task
                finish_analysis_progress(
                    progress, ok=bool(outcome.ok), error_type=getattr(outcome, "error_type", ""),
                    model_name=getattr(progress, "model_name", ""),
                )
                return outcome
            if repository.is_cancel_requested(run_id):
                logger.info("cancelling active KkAgent for daily brief run %s", run_id)
                analysis_task.cancel()
                with suppress(asyncio.CancelledError):
                    await analysis_task
                finish_analysis_progress(progress, ok=False, cancelled=True)
                raise RunCancelledError()
    except asyncio.CancelledError:
        if not analysis_task.done():
            analysis_task.cancel()
        with suppress(asyncio.CancelledError):
            await analysis_task
        finish_analysis_progress(progress, ok=False, cancelled=True)
        raise


async def gather_cancel_on_error(coroutines: list[Any]) -> None:
    """Gather issue work and clean up every sibling when one task aborts."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise


def reset_cancelled_issue(repository: Any, record: Any) -> None:
    """Return an interrupted issue to a clean, explicitly retryable state."""
    record.status = "pending"
    record.started_at = ""
    record.finished_at = ""
    record.duration_ms = 0
    record.error = ""
    record.error_type = ""
    repository.upsert_issue(record)


def mark_reanalysis_cancelled(repository: Any, run: Any, issue_id: int) -> dict[str, Any]:
    """Converge a cancelled single-issue job without classifying it as failure.

    语义（2850cff 定版）：被停止的单条分析收敛为 ``cancelled``——它与
    run 终态一致，明确表达"最新一次尝试被用户停止"，且不会把已取消 run
    下的条目伪装成待执行；重新分析会新建 issue job 并覆盖该状态。
    """
    record = repository.get_issue(run.run_id, issue_id)
    if record is not None:
        record.status = "cancelled"
        record.finished_at = _now()
        record.error = ""
        record.error_type = ""
        repository.upsert_issue(record)
    run.status = "cancelled"
    run.error = ""
    run.finished_at = _now()
    repository.update_run(run)
    return {"run_id": run.run_id, "issue_id": issue_id, "status": "cancelled"}


__all__ = [
    "RunCancelledError",
    "analyze_with_persisted_cancel",
    "gather_cancel_on_error",
    "mark_reanalysis_cancelled",
    "reset_cancelled_issue",
]
