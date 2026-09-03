"""foundation.loop_watchdog 行为测试。

看门狗价值在"事件循环卡死时仍能 dump 线程栈"（faulthandler C 级定时器）。
单测验证：健康循环下启停干净；循环被同步阻塞时会记录 lag 告警。
"""

from __future__ import annotations

import asyncio
import logging
import time
import unittest

from foundation import loop_watchdog


class LoopWatchdogTests(unittest.TestCase):
    def test_start_then_stop_is_clean(self):
        async def scenario():
            task = await loop_watchdog.start_loop_watchdog()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await loop_watchdog.stop_loop_watchdog(task)
            self.assertTrue(task.cancelled() or task.done())

        asyncio.run(scenario())

    def test_loop_stall_logs_lag_warning(self):
        async def scenario():
            # 把重武装间隔调小，阈值调低，避免测试变慢。
            rearm = 0.05
            threshold = 0.02
            logs = []

            class _Collector(logging.Handler):
                def emit(self, record):
                    logs.append(record.getMessage())

            handler = _Collector()
            logger = logging.getLogger("foundation.loop_watchdog")
            logger.addHandler(handler)
            old_threshold = loop_watchdog.LOOP_LAG_LOG_THRESHOLD
            old_rearm = loop_watchdog.REARM_INTERVAL_SECONDS
            loop_watchdog.LOOP_LAG_LOG_THRESHOLD = threshold
            loop_watchdog.REARM_INTERVAL_SECONDS = rearm
            try:
                task = asyncio.create_task(loop_watchdog._watch())
                # 先让看门狗完成一次正常 re-arm（覆盖 dump_traceback_later
                # 的武装与取消路径），再人为同步阻塞循环制造 lag。
                await asyncio.sleep(rearm * 2)
                time.sleep(rearm * 4 + threshold)
                await asyncio.sleep(rearm * 2)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                loop_watchdog.LOOP_LAG_LOG_THRESHOLD = old_threshold
                loop_watchdog.REARM_INTERVAL_SECONDS = old_rearm
                logger.removeHandler(handler)

            self.assertTrue(
                any("event loop lag" in message for message in logs),
                logs,
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
