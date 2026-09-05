"""烧写等长命令的过程日志上报队列。

设计目标：日志上报绝不能成为设备烧写关键路径的一部分。

- 输出回调线程只做 ``put_nowait()``，永远不做 HTTP；
- 独立 uploader 线程批量上传（默认 5s 超时 + 指数退避），
  失败时把批次重新入队，保证"只有上传成功才丢弃"；
- ``flush()`` 在烧写收尾时有界等待排空，不会无限阻塞任务收尾。
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


class CommandEventUploader:
    """后台批量上传 command events；回调线程只入队。"""

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
        self._thread = threading.Thread(
            target=self._run, name="command-event-uploader", daemon=True)
        self._thread.start()

    def submit(self, event: dict[str, Any]) -> None:
        """入队单条事件；队列满时丢弃最旧事件（日志不能拖垮烧写）。"""
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

    def flush(self) -> None:
        """有界等待排空队列（烧写收尾时调用一次）。"""
        deadline = time.monotonic() + _FLUSH_DRAIN_TIMEOUT_SECONDS
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.1)
        self._stopped.set()
        self._thread.join(timeout=2.0)
        if self._dropped:
            logger.warning(
                "command event queue overflow: dropped %d oldest events",
                self._dropped)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stopped.is_set() or not self._queue.empty():
            batch = self._collect_batch()
            if not batch:
                self._stopped.wait(0.2)
                continue
            try:
                self._upload(batch)
                backoff = 1.0
            except Exception as exc:
                # 失败重入队：只有上传成功才丢弃；指数退避避免打爆 Controller。
                self._requeue(batch)
                logger.warning(
                    "command event upload failed (%s); requeued %d, retry in %.1fs",
                    exc, len(batch), backoff)
                self._stopped.wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

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

    def _requeue(self, batch: list[dict[str, Any]]) -> None:
        for event in reversed(batch):
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._dropped += len(batch) - batch.index(event)
                break
