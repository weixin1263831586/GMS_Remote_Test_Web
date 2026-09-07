"""烧写等长命令的过程日志上报队列。

设计目标：日志上报绝不能成为设备烧写关键路径的一部分，同时事件
sequence 必须严格单调递增地到达 Controller。

- 输出回调线程只做 ``put_nowait()``，永远不做 HTTP；
- 独立 uploader 线程批量上传（默认 5s 超时 + 指数退避）。失败 batch
  **原地重试**（pending_batch 占住队头，绝不 dequeue 后续 batch）——
  若失败后塞回公共队列尾部，50~99 会先于 0~49 到达，浏览器 cursor
  （max sequence）之后永远拉不到 0~49，烧写开头的日志会永久丢失；
- ``flush()`` 等待 queue 排空 **且** inflight batch 已 ACK，
  保证返回后日志真正落 Controller，随后 shutdown/uninstall 不掉尾部。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 5
_FLUSH_DRAIN_TIMEOUT_SECONDS = 30
_MAX_BACKOFF_SECONDS = 30
_MAX_QUEUE_EVENTS = 20000


class _InflightCounter:
    """跟踪正在上传、尚未 ACK 的 batch 数（0 或 1）。"""

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()
        self._zero = threading.Condition(self._lock)

    def enter(self) -> None:
        with self._lock:
            self._count += 1

    def leave(self) -> None:
        with self._lock:
            self._count -= 1
            if self._count <= 0:
                self._zero.notify_all()

    def is_zero(self) -> bool:
        with self._lock:
            return self._count <= 0

    def wait_zero(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._count > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._zero.wait(remaining)
            return True


class CommandEventUploader:
    """后台批量上传 command events；失败 batch 原地重试保证严格有序。"""

    def __init__(
        self,
        upload: Callable[[list[dict[str, Any]]], Any],
        batch_size: int = 50,
    ):
        self._upload = upload
        self._batch_size = batch_size
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=_MAX_QUEUE_EVENTS)
        self._stopped = threading.Event()
        self._dropped = 0
        self._inflight = _InflightCounter()
        self._thread = threading.Thread(
            target=self._run, name="command-event-uploader", daemon=True)
        self._thread.start()

    def submit(self, event: dict[str, Any]) -> None:
        """入队单条事件；队列满时丢弃最旧事件（日志不能拖垮烧写）。"""
        if self._stopped.is_set():
            # flush() 之后 uploader 已停：迟到事件无处投递，丢弃并计数
            # （正常烧写流程 flush 是收尾最后一步，不应再有 submit）。
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(event)
            except queue.Full:  # pragma: no cover - 理论不可达
                pass

    def flush(self, timeout: float = _FLUSH_DRAIN_TIMEOUT_SECONDS) -> None:
        """有界等待"队列排空且 in-flight batch 已 ACK"（真·drain）。

        只等 queue.empty() 不够：uploader 可能已 dequeue 一批但 HTTP 仍
        in-flight（最长 ~5s）。flush 必须等到 inflight 也清零，Worker
        随后立即 shutdown/uninstall 时最后一批日志才不会掉。

        超时放弃时仍会置位 stopped 结束线程；uploader 线程对未 ACK 的
        pending batch 做最后一次无重试投递（见 _run），尽力保住事件。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._queue.empty():
            time.sleep(0.05)
        drained = self._inflight.wait_zero(
            max(0.0, deadline - time.monotonic()))
        self._stopped.set()
        self._thread.join(timeout=2.0)
        if not drained and not self._queue.empty():
            logger.warning(
                "command event flush timed out; %d events still queued",
                self._queue.qsize())
        if self._dropped:
            logger.warning(
                "command event queue overflow: dropped %d oldest events",
                self._dropped)

    def _run(self) -> None:
        pending_batch: list[dict[str, Any]] | None = None
        backoff = 1.0
        while True:
            if self._stopped.is_set() and not pending_batch and self._queue.empty():
                return
            if pending_batch is None:
                pending_batch = self._collect_batch()
                if not pending_batch:
                    if self._stopped.wait(0.2):
                        # stopped 置位后再检查一轮，确保排空语义。
                        if self._queue.empty():
                            return
                    continue
            self._inflight.enter()
            try:
                self._upload(pending_batch)
                pending_batch = None
                backoff = 1.0
            except Exception as exc:
                # 失败原地重试：pending_batch 保持队头语义，绝不让
                # 后续 batch 越过它，保证 sequence 严格单调到达。
                logger.warning(
                    "command event upload failed (%s); retrying %d events in %.1fs",
                    exc, len(pending_batch), backoff)
                if self._stopped.wait(backoff):
                    # flush() 超时放弃等待：做最后一次无重试投递，尽力
                    # 保住队头事件；仍失败则计入 dropped（证据不静默丢失）。
                    try:
                        self._upload(pending_batch)
                    except Exception as final_exc:
                        self._dropped += len(pending_batch)
                        logger.warning(
                            "command event final upload failed (%s); %d events dropped",
                            final_exc, len(pending_batch))
                    pending_batch = None
                    return
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
            finally:
                self._inflight.leave()

    def _collect_batch(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        try:
            batch.append(self._queue.get(timeout=0.5))
        except queue.Empty:
            return batch
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch
