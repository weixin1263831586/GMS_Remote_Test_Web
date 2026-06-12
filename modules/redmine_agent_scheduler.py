"""Daily scheduler for RedmineAgent."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

from core.config import config_manager

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_last_run_day: str = ""


def _load_scheduler_config() -> Dict[str, Any]:
    """Load scheduler settings from config.json redmine_agent section, with env overrides."""
    cfg = config_manager.load_config().get("redmine_agent", {})
    return {
        "scan_hour": int(os.getenv("REDMINE_AGENT_SCAN_HOUR", cfg.get("scan_hour", 0))),
        "scan_minute": int(os.getenv("REDMINE_AGENT_SCAN_MINUTE", cfg.get("scan_minute", 0))),
        "scan_hours_window": int(os.getenv("REDMINE_AGENT_SCAN_HOURS", cfg.get("scan_hours_window", 24))),
        "scan_max_issues": int(os.getenv("REDMINE_AGENT_MAX_ISSUES", cfg.get("max_issues_per_run", 50))),
    }


async def _loop() -> None:
    global _last_run_day
    while True:
        try:
            sc = _load_scheduler_config()
            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            if now.hour == sc["scan_hour"] and now.minute == sc["scan_minute"] and _last_run_day != day:
                _last_run_day = day
                logger.info(
                    "[RedmineAgentScheduler] starting scheduled scan (hour=%d, minute=%d, window=%dh, max=%d)",
                    sc["scan_hour"], sc["scan_minute"], sc["scan_hours_window"], sc["scan_max_issues"],
                )
                try:
                    from routers.redmine_agent import start_redmine_agent_run
                    result = await start_redmine_agent_run(
                        hours=sc["scan_hours_window"],
                        max_issues=sc["scan_max_issues"],
                        mode="scheduled",
                    )
                    logger.info("[RedmineAgentScheduler] scan start result: %s", result)
                except Exception as exc:
                    logger.error("[RedmineAgentScheduler] scan failed to start: %s", exc, exc_info=True)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[RedmineAgentScheduler] loop error: %s", exc, exc_info=True)
            await asyncio.sleep(60)


def start_redmine_agent_scheduler() -> Optional[asyncio.Task]:
    global _task
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(_loop())
    sc = _load_scheduler_config()
    logger.info(
        "[RedmineAgentScheduler] started (scan at %02d:%02d, window=%dh, max=%d)",
        sc["scan_hour"], sc["scan_minute"], sc["scan_hours_window"], sc["scan_max_issues"],
    )
    return _task


async def stop_redmine_agent_scheduler() -> None:
    global _task
    if not _task:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    logger.info("[RedmineAgentScheduler] stopped")


def get_scheduler_config() -> dict:
    """Return current scheduler configuration."""
    sc = _load_scheduler_config()
    return {
        **sc,
        "running": _task is not None and not _task.done(),
    }
