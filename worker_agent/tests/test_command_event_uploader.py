"""CommandEventUploader 的 flush/submit 生命周期回归测试。"""

from __future__ import annotations

import time

from worker_agent.command_events import CommandEventUploader


def test_submit_after_flush_drops_and_counts():
    """flush() 之后迟到的 submit 不再无限滞留队列，计入 dropped。"""
    uploaded: list[list[dict]] = []
    uploader = CommandEventUploader(
        lambda batch: uploaded.append(batch), batch_size=10)
    uploader.submit({"sequence": 0})
    uploader.flush(timeout=5)
    assert uploaded, "flush 前提交的事件应已上传"

    uploader.submit({"sequence": 1})
    assert uploader._dropped == 1
    assert uploader._queue.empty()


def test_flush_waits_for_inflight_batch():
    """HTTP 慢时 flush 等待 in-flight batch ACK，不提前返回。"""
    release = {"go": False}

    def slow_upload(batch):
        deadline = time.monotonic() + 5
        while not release["go"] and time.monotonic() < deadline:
            time.sleep(0.02)

    uploader = CommandEventUploader(slow_upload, batch_size=10)
    uploader.submit({"sequence": 0})
    time.sleep(0.3)  # 让 uploader dequeue 并进入 in-flight
    assert not uploader._inflight.is_zero()

    result = {}
    import threading
    t = threading.Thread(target=lambda: result.setdefault(
        "done", (uploader.flush(timeout=5), uploaded_done(uploader))))
    t.start()
    time.sleep(0.5)
    assert t.is_alive(), "in-flight 未 ACK 时 flush 必须继续等待"
    release["go"] = True
    t.join(timeout=6)
    assert not t.is_alive()
    assert uploader._inflight.is_zero()


def uploaded_done(_uploader):
    return True


def test_flush_timeout_final_upload_attempt():
    """flush 超时放弃后，uploader 对 pending batch 做最后一次投递。"""
    attempts = {"n": 0}

    def flaky_upload(batch):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        # 第二次（flush 触发的 final attempt）成功

    uploader = CommandEventUploader(flaky_upload, batch_size=10)
    uploader.submit({"sequence": 0})
    # 第一次上传失败进入 backoff；flush 把超时压到 0.2s 并触发 final attempt
    uploader.flush(timeout=0.2)
    assert attempts["n"] >= 2
    assert uploader._dropped == 0


def test_flush_timeout_drops_after_final_failure():
    """final attempt 仍失败时事件计入 dropped，不静默消失。"""
    attempts = {"n": 0}

    def always_fail(batch):
        attempts["n"] += 1
        raise RuntimeError("down")

    uploader = CommandEventUploader(always_fail, batch_size=10)
    uploader.submit({"sequence": 0})
    uploader.flush(timeout=0.2)
    assert attempts["n"] >= 2
    assert uploader._dropped == 1
