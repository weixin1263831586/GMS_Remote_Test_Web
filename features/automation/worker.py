"""后台 worker 驱动自动化流水线。

定期执行状态机推进和构建任务轮询，重操作通过 asyncio.to_thread
在独立线程运行避免阻塞事件循环。
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
_advancement_task: asyncio.Task | None = None
_stale_swept: bool = False
_last_build_poll_at: float = 0.0
_last_tick_at: float = 0.0
_last_tick_result: dict[str, Any] = {}


def _load_worker_config() -> dict[str, Any]:
    """从 config.json 加载 automation_worker 配置，支持环境变量覆盖。"""
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
    """惰性解析模块级 service 单例。"""
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
    """将重启前遗留的 in-flight 构建任务和自动化 run 标记为失败/取消。"""
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
    """轮询 running 构建任务的远程 .done 文件并更新状态。"""
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
        # 从 waiting_device 状态进入事件开始计时，不受轮询刷新影响
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
    """返回当前阶段的停留时间，不受轮询事件影响。"""
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
    """取消超时可安全中断的 ATS 阶段。"""
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


def run_maintenance_sync(
    cfg: dict[str, Any],
    *,
    automation_service: Any | None = None,
    build_service: Any | None = None,
) -> dict[str, Any]:
    """轮询构建任务和超时清理，独立于 run 推进。"""
    auto_svc = automation_service
    build_svc = build_service
    if auto_svc is None or build_svc is None:
        auto_svc, build_svc = _resolve_services()
    if auto_svc is None or build_svc is None:
        return {
            "polled_builds": 0,
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

    return {
        "polled_builds": polled_builds,
        "device_timeouts": device_timeouts,
        "stage_timeouts": stage_timeouts,
    }


def advance_runs_sync(
    cfg: dict[str, Any],
    *,
    automation_service: Any | None = None,
) -> dict[str, int]:
    """在单航班长任务通道中推进持久化 run。"""

    auto_svc = automation_service or _resolve_services()[0]
    if auto_svc is None:
        return {"advanced_runs": 0}
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
        # 轮询阶段会返回相同的持久状态，避免对同一 API 连续调用 25 次
        if signature == previous_signature:
            break
        previous_signature = signature

    return {"advanced_runs": advanced_runs}


def run_tick_sync(
    cfg: dict[str, Any],
    *,
    automation_service: Any | None = None,
    build_service: Any | None = None,
) -> dict[str, Any]:
    """兼容/手动 tick，合并维护和推进。"""

    maintenance = run_maintenance_sync(
        cfg,
        automation_service=automation_service,
        build_service=build_service,
    )
    advancement = advance_runs_sync(
        cfg,
        automation_service=automation_service,
    )
    return {**maintenance, **advancement}


async def _tick_once(cfg: dict[str, Any]) -> None:
    global _advancement_task, _last_tick_at, _last_tick_result
    # 维护任务持续运行，同时长推进任务（如刷机/分析）占用单航班通道
    maintenance = await asyncio.to_thread(run_maintenance_sync, cfg)
    completed_advancement: dict[str, Any] = {}
    if _advancement_task and _advancement_task.done():
        try:
            completed_advancement = _advancement_task.result()
        except asyncio.CancelledError:
            completed_advancement = {}
        except Exception:
            logger.exception("[AutomationWorker] advancement lane failed")
            completed_advancement = {"advancement_error": True}
        _advancement_task = None
    if _advancement_task is None:
        _advancement_task = asyncio.create_task(
            asyncio.to_thread(advance_runs_sync, cfg)
        )
    _last_tick_at = asyncio.get_event_loop().time()
    _last_tick_result = {
        **maintenance,
        **completed_advancement,
        "advancement_running": not _advancement_task.done(),
    }


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
    global _advancement_task, _last_build_poll_at, _stale_swept, _task
    if _task and not _task.done():
        return _task
    cfg = _load_worker_config()
    if not cfg["enabled"]:
        logger.info("[AutomationWorker] disabled by config; not starting")
        return None
    _stale_swept = False
    _last_build_poll_at = 0.0
    _advancement_task = None
    _task = asyncio.create_task(_loop())
    return _task


async def stop_automation_worker() -> None:
    global _advancement_task, _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    if _advancement_task:
        _advancement_task.cancel()
        try:
            await _advancement_task
        except asyncio.CancelledError:
            pass
        _advancement_task = None
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
        "advancement_running": (
            _advancement_task is not None and not _advancement_task.done()
        ),
    }
