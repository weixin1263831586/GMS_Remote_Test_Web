"""Event-loop stall watchdog.

2026-09-03 事故复盘：源端固件烧写（SFTP 上传 4.6GB 固件到 Windows 源主机）
期间，Controller 的 asyncio 事件循环被未知同步调用阻塞约 5 分钟，全部
HTTP 请求（含其他机器的集群轮询）超时，最终靠重启服务恢复。静态审计中
烧写/重连路径均已正确使用 ``asyncio.to_thread`` / 独立线程，无法定位
卡死点。

本模块提供下次复发的现场捕获能力：``faulthandler.dump_traceback_later``
由 C 级定时器线程驱动，不依赖 Python 事件循环调度——即使循环完全卡死，
超时后仍会把**全部线程栈** dump 到 stderr（journald 收集），其中事件循环
线程的栈顶即卡死现场。健康循环每 ``REARM_INTERVAL_SECONDS`` 秒取消并
重新武装定时器，正常运行时永不触发。
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import time

logger = logging.getLogger(__name__)

DUMP_TIMEOUT_SECONDS = 30.0
REARM_INTERVAL_SECONDS = 5.0
LOOP_LAG_LOG_THRESHOLD = 3.0


async def _watch() -> None:
    while True:
        faulthandler.dump_traceback_later(DUMP_TIMEOUT_SECONDS, exit=False)
        started = time.monotonic()
        await asyncio.sleep(REARM_INTERVAL_SECONDS)
        lag = time.monotonic() - started - REARM_INTERVAL_SECONDS
        if lag > LOOP_LAG_LOG_THRESHOLD:
            logger.error(
                "[LoopWatchdog] event loop lag %.1fs (threshold %.1fs); "
                "full thread dump follows if the stall reaches %.0fs",
                lag,
                LOOP_LAG_LOG_THRESHOLD,
                DUMP_TIMEOUT_SECONDS,
            )


async def start_loop_watchdog() -> asyncio.Task:
    """Arm the stall detector; the returned task keeps re-arming it."""

    task = asyncio.create_task(_watch())
    logger.info(
        "[LoopWatchdog] started: dumps all thread stacks when the loop "
        "stalls over %.0fs",
        DUMP_TIMEOUT_SECONDS,
    )
    return task


async def stop_loop_watchdog(task: asyncio.Task | None) -> None:
    """Cancel the watchdog and disarm any pending traceback dump."""

    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    faulthandler.cancel_dump_traceback_later()
