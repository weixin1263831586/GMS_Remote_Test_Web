"""Background worker that drives the automation pipeline.

Mirrors ``features/redmine/scheduler.py``: an asyncio task that periodically
runs the automation state machine and polls in-flight build jobs, so runs
advance without a human clicking "Worker Tick". Everything heavy
(``worker_tick`` can flash devices for up to 3600s) runs in a worker thread
via :func:`asyncio.to_thread` so the event loop is never blocked.

``automation_service`` / ``build_service`` are module-level singletons set
during route inclusion, which happens before the lifespan (and thus this
loop) starts. We re-resolve them lazily on every tick rather than capturing a
reference at startup, matching how :class:`HttpAutomationExecutor` already
resolves ``build_service``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from foundation.config import config_manager


logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stale_swept: bool = False
_last_build_poll_at: float = 0.0
_last_tick_at: float = 0.0
_last_tick_result: dict[str, Any] = {}


def _load_worker_config() -> dict[str, Any]:
    """Load automation_worker settings from config.json with env overrides."""
    cfg = config_manager.load_config().get("automation_worker", {})
    return {
        "enabled": os.getenv("ATS_WORKER_ENABLED", str(cfg.get("enabled", True))).lower()
        not in ("0", "false", "no", "off"),
        "interval_seconds": max(1, int(os.getenv("ATS_WORKER_INTERVAL", cfg.get("interval_seconds", 15)))),
        "build_poll_interval_seconds": max(1, int(
            os.getenv("ATS_WORKER_BUILD_POLL_INTERVAL", cfg.get("build_poll_interval_seconds", 10))
        )),
        "executor": os.getenv("ATS_WORKER_EXECUTOR", cfg.get("executor", "http")),
        "stale_build_seconds": max(1, int(os.getenv("ATS_STALE_BUILD_SECONDS", cfg.get("stale_build_seconds", 7200)))),
        "stale_run_seconds": max(1, int(os.getenv("ATS_STALE_RUN_SECONDS", cfg.get("stale_run_seconds", 86400)))),
        "waiting_device_timeout_seconds": max(1, int(
            os.getenv("ATS_WAITING_DEVICE_TIMEOUT", cfg.get("waiting_device_timeout_seconds", 1800))
        )),
        "test_timeout_seconds": max(1, int(
            os.getenv("ATS_TEST_TIMEOUT", cfg.get("test_timeout_seconds", 86400))
        )),
        "report_collection_timeout_seconds": max(1, int(
            os.getenv(
                "ATS_REPORT_COLLECTION_TIMEOUT",
                cfg.get("report_collection_timeout_seconds", 1800),
            )
        )),
    }


def _resolve_services():
    """Lazily resolve the module-level service singletons.

    Returns ``(automation_service, build_service)`` or ``(None, None)`` if the
    API modules have not been configured yet.
    """
    try:
        from features.automation.api import automation_service
        from features.build import get_build_service
    except Exception:  # pragma: no cover - import guard
        return None, None
    return automation_service, get_build_service()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _age_seconds(value: str) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stale_sweep_once(
    cfg: dict[str, Any],
    *,
    automation_service: Any | None = None,
    build_service: Any | None = None,
) -> dict[str, Any]:
    """Mark pre-existing in-flight jobs/runs as failed/cancelled.

    Runs once per process (guarded by ``_stale_swept`` from the loop) so a
    restart after a crash does not leave orphaned ``running`` build jobs and
    half-advanced automation runs that the new worker would otherwise never
    reconcile.
    """
    auto_svc = automation_service
    build_svc = build_service
    if auto_svc is None or build_svc is None:
        auto_svc, build_svc = _resolve_services()
    if auto_svc is None or build_svc is None:
        return {"builds_marked": 0, "runs_cancelled": 0}

    from features.automation.models import TERMINAL_STATUSES
    from features.build import JOB_FAILED, JOB_QUEUED, JOB_RUNNING

    threshold_build = cfg["stale_build_seconds"]
    threshold_run = cfg["stale_run_seconds"]
    builds_marked = 0
    runs_cancelled = 0

    for status in (JOB_RUNNING, JOB_QUEUED):
        for job in build_svc.list_jobs(status=status, limit=500):
            age = _age_seconds(job.get("started_at") or job.get("created_at") or "")
            if age is not None and age > threshold_build:
                build_svc.store.update_job(
                    job["id"],
                    status=JOB_FAILED,
                    error="stale before worker start",
                    finished_at=_utc_now_iso(),
                )
                builds_marked += 1

    for run in auto_svc.list_runs(limit=500):
        if run["status"] in TERMINAL_STATUSES:
            continue
        age = _age_seconds(run.get("updated_at") or run.get("created_at") or "")
        if age is not None and age > threshold_run:
            try:
                auto_svc.orchestrator(cfg["executor"]).cancel_run(
                    run["id"], reason="stale before worker start", cleanup=True
                )
                runs_cancelled += 1
            except Exception:
                logger.exception("failed to cancel stale run %s", run.get("id"))

    logger.info(
        "[AutomationWorker] stale sweep marked %d build job(s), cancelled %d run(s)",
        builds_marked,
        runs_cancelled,
    )
    return {"builds_marked": builds_marked, "runs_cancelled": runs_cancelled}


def poll_running_builds_sync(build_service: Any | None = None) -> int:
    """Probe every running build job's remote ``.done`` file and update status.

    This is the missing piece today: nothing polled the 4 stuck ``running``
    jobs, so finished remote builds never transitioned. Returns the number of
    jobs polled.
    """
    build_svc = build_service or _resolve_services()[1]
    if build_svc is None:
        return 0
    # Promote queued jobs whose server now has free capacity.
    try:
        build_svc.start_queued_jobs()
    except Exception:
        logger.exception("failed to promote queued build jobs")
    polled = 0
    for job in build_svc.list_jobs(status="running", limit=100):
        try:
            build_svc.poll_job(job["id"])
            polled += 1
        except Exception:
            logger.exception("build poll failed for %s", job.get("id"))
    return polled


def sweep_waiting_device_timeouts(
    cfg: dict[str, Any],
    *,
    automation_service: Any | None = None,
) -> int:
    """Cancel ``waiting_device`` runs that have waited past the timeout."""
    auto_svc = automation_service or _resolve_services()[0]
    if auto_svc is None:
        return 0
    timeout = cfg["waiting_device_timeout_seconds"]
    cancelled = 0
    for run in auto_svc.list_runs(status="waiting_device", limit=100):
        # Poll retries touch updated_at for fair scheduling, so timeout from
        # the first waiting_device event (the status-entry transition).
        entered_at = ""
        try:
            entered_at = next(
                (
                    event.get("created_at") or ""
                    for event in auto_svc.list_events(run["id"])
                    if event.get("stage") == "waiting_device"
                ),
                "",
            )
        except Exception:
            logger.debug("failed to resolve waiting_device entry time", exc_info=True)
        age = _age_seconds(entered_at or run.get("updated_at") or "")
        if age is not None and age > timeout:
            try:
                auto_svc.orchestrator(cfg["executor"]).cancel_run(
                    run["id"], reason="device selection timed out"
                )
                cancelled += 1
            except Exception:
                logger.exception("failed to cancel waiting_device run %s", run.get("id"))
    return cancelled


def _stage_entry_age_seconds(
    automation_service: Any, run: dict[str, Any], stage: str
) -> float | None:
    """Return time in the current stage without being reset by poll events."""
    entered_at = ""
    try:
        entered_at = next(
            (
                event.get("created_at") or ""
                for event in automation_service.list_events(run["id"])
                if event.get("stage") == stage
            ),
            "",
        )
    except Exception:
        logger.debug("failed to resolve %s entry time", stage, exc_info=True)
    return _age_seconds(entered_at or run.get("updated_at") or "")


def sweep_active_stage_timeouts(
    cfg: dict[str, Any],
    *,
    automation_service: Any | None = None,
) -> dict[str, int]:
    """Cancel safely interruptible ATS stages that exceed their wall timeout."""
    auto_svc = automation_service or _resolve_services()[0]
    counts = {"test_running": 0, "report_collecting": 0}
    if auto_svc is None:
        return counts

    stages = (
        (
            "test_running",
            int(cfg.get("test_timeout_seconds", cfg.get("stale_run_seconds", 86400))),
            "GMS test timed out",
        ),
        (
            "report_collecting",
            int(cfg.get("report_collection_timeout_seconds", 1800)),
            "report collection timed out",
        ),
    )
    for stage, timeout, reason in stages:
        for run in auto_svc.list_runs(status=stage, limit=500):
            age = _stage_entry_age_seconds(auto_svc, run, stage)
            if age is None or age <= timeout:
                continue
            try:
                auto_svc.orchestrator(cfg["executor"]).cancel_run(
                    run["id"], reason=f"{reason} after {timeout}s", cleanup=True
                )
                counts[stage] += 1
            except Exception:
                logger.exception("failed to cancel timed-out %s run %s", stage, run.get("id"))
    return counts


def run_tick_sync(
    cfg: dict[str, Any],
    *,
    automation_service: Any | None = None,
    build_service: Any | None = None,
) -> dict[str, Any]:
    """One synchronous tick — runs in a worker thread, never on the loop."""
    auto_svc = automation_service
    build_svc = build_service
    if auto_svc is None or build_svc is None:
        auto_svc, build_svc = _resolve_services()
    if auto_svc is None or build_svc is None:
        return {
            "polled_builds": 0,
            "advanced_runs": 0,
            "device_timeouts": 0,
            "stage_timeouts": {"test_running": 0, "report_collecting": 0},
            "skipped": True,
        }

    global _stale_swept
    if not _stale_swept:
        try:
            stale_sweep_once(cfg, automation_service=auto_svc, build_service=build_svc)
        except Exception:
            logger.exception("[AutomationWorker] stale sweep failed")
        _stale_swept = True

    global _last_build_poll_at
    polled_builds = 0
    build_poll_interval = max(1, int(cfg.get("build_poll_interval_seconds", 10)))
    now = time.monotonic()
    if not _last_build_poll_at or now - _last_build_poll_at >= build_poll_interval:
        _last_build_poll_at = now
        try:
            polled_builds = poll_running_builds_sync(build_svc)
        except Exception:
            logger.exception("[AutomationWorker] build poll loop failed")

    device_timeouts = 0
    try:
        device_timeouts = sweep_waiting_device_timeouts(cfg, automation_service=auto_svc)
    except Exception:
        logger.exception("[AutomationWorker] waiting_device sweep failed")

    stage_timeouts = {"test_running": 0, "report_collecting": 0}
    try:
        stage_timeouts = sweep_active_stage_timeouts(cfg, automation_service=auto_svc)
    except Exception:
        logger.exception("[AutomationWorker] active-stage timeout sweep failed")

    # Advance runs. Cap iterations per tick so a single tick (which may block
    # inside a 3600s flash) does not loop indefinitely. worker_tick returns
    # None when no non-terminal run remains.
    advanced_runs = 0
    cap = 25
    previous_signature: tuple[str, str] | None = None
    while advanced_runs < cap:
        try:
            advanced = auto_svc.worker_tick(cfg["executor"])
        except Exception:
            logger.exception("[AutomationWorker] worker_tick failed")
            break
        if not advanced:
            break
        advanced_runs += 1
        signature = (str(advanced.get("id") or ""), str(advanced.get("status") or ""))
        # Polling stages deliberately return the same durable state while the
        # external build/test is still running. Do not hammer the same API 25
        # times in one worker tick; the next scheduled tick will poll again.
        if signature == previous_signature:
            break
        previous_signature = signature

    return {
        "polled_builds": polled_builds,
        "advanced_runs": advanced_runs,
        "device_timeouts": device_timeouts,
        "stage_timeouts": stage_timeouts,
    }


async def _tick_once(cfg: dict[str, Any]) -> None:
    global _last_tick_at, _last_tick_result
    # Run the blocking tick off the event loop — flash can take up to 3600s.
    result = await asyncio.to_thread(run_tick_sync, cfg)
    _last_tick_at = asyncio.get_event_loop().time()
    _last_tick_result = result


async def _loop() -> None:
    global _stale_swept
    cfg = _load_worker_config()
    logger.info(
        "[AutomationWorker] started (interval=%ds, executor=%s)",
        cfg["interval_seconds"],
        cfg["executor"],
    )
    while True:
        try:
            cfg = _load_worker_config()
            if cfg["enabled"]:
                await _tick_once(cfg)
            await asyncio.sleep(cfg["interval_seconds"])
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[AutomationWorker] loop error")
            await asyncio.sleep(60)


def start_automation_worker() -> asyncio.Task | None:
    global _last_build_poll_at, _stale_swept, _task
    if _task and not _task.done():
        return _task
    cfg = _load_worker_config()
    if not cfg["enabled"]:
        logger.info("[AutomationWorker] disabled by config; not starting")
        return None
    _stale_swept = False
    _last_build_poll_at = 0.0
    _task = asyncio.create_task(_loop())
    return _task


async def stop_automation_worker() -> None:
    global _task
    if not _task:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    logger.info("[AutomationWorker] stopped")


def get_worker_status() -> dict[str, Any]:
    cfg = _load_worker_config()
    last_ago = (time.monotonic() - _last_tick_at) if _last_tick_at else None
    return {
        "running": _task is not None and not _task.done(),
        "enabled": cfg["enabled"],
        "interval_seconds": cfg["interval_seconds"],
        "executor": cfg["executor"],
        "last_tick_result": _last_tick_result,
        "last_tick_seconds_ago": round(last_ago, 1) if last_ago is not None else None,
    }
