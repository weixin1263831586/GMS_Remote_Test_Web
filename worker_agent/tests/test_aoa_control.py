"""AOA 回放过程控制注册表（worker_agent.aoa_control）单测。

覆盖：pause→resume 门控语义、cancel 唤醒暂停等待并抛异常、
预置请求（控制先于注册到达）、TTL 兜底清理。
"""
import time
import unittest

from worker_agent import aoa_control
from worker_agent.aoa_control import AoaPlaybackCancelledError


class _Clock:
    """可手动推进的单调钟 + 记录型 sleep。"""

    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class AoAControlTests(unittest.TestCase):
    def setUp(self):
        aoa_control._CONTROLS.clear()

    def tearDown(self):
        aoa_control._CONTROLS.clear()

    def test_pause_resume_gate_blocks_until_resumed(self):
        clock = _Clock()
        events: list[dict] = []
        aoa_control.register("ctl-1")
        aoa_control.request("ctl-1", "pause")
        # 模拟另一线程在两次 poll 之后发出 resume。
        original_sleep = clock.sleep

        def resume_midway(seconds: float) -> None:
            original_sleep(seconds)
            if len(clock.sleeps) == 2:
                aoa_control.request("ctl-1", "resume")

        clock.sleep = resume_midway
        paused_for = aoa_control.wait_if_paused(
            "ctl-1", index=3, total=10, sleep=clock.sleep,
            poll_seconds=0.5,
            on_event=lambda e: events.append(e["event_type"]))
        self.assertGreater(paused_for, 0)
        self.assertEqual(events, ["paused", "resumed"])
        clock.sleeps.clear()
        self.assertEqual(aoa_control.wait_if_paused(
            "ctl-1", index=4, total=10, sleep=clock.sleep), 0.0)
        self.assertEqual(clock.sleeps, [])

    def test_cancel_wakes_paused_wait_and_raises(self):
        clock = _Clock()
        aoa_control.register("ctl-2")
        aoa_control.request("ctl-2", "pause")
        # 模拟另一线程在两次 poll 之间发出 cancel。
        original_sleep = clock.sleep

        def cancel_midway(seconds: float) -> None:
            original_sleep(seconds)
            if len(clock.sleeps) == 2:
                aoa_control.request("ctl-2", "cancel")

        clock.sleep = cancel_midway
        with self.assertRaises(AoaPlaybackCancelledError) as ctx:
            aoa_control.wait_if_paused("ctl-2", index=5, total=10,
                                       sleep=clock.sleep, poll_seconds=0.5)
        self.assertEqual(ctx.exception.index, 5)
        self.assertEqual(ctx.exception.total, 10)

    def test_cancel_before_gate_raises_immediately(self):
        clock = _Clock()
        aoa_control.request("ctl-3", "cancel")  # 预置：先于注册到达
        with self.assertRaises(AoaPlaybackCancelledError):
            aoa_control.wait_if_paused("ctl-3", index=1, total=2,
                                       sleep=clock.sleep)
        # 预置请求同样支持 pause 预置（第一步即暂停）。
        aoa_control.request("ctl-4", "pause")
        self.assertEqual(aoa_control.status("ctl-4"), {"paused": True,
                                                       "cancelled": False})

    def test_unsupported_action_rejected(self):
        with self.assertRaises(ValueError):
            aoa_control.request("ctl-5", "rewind")

    def test_ttl_purges_stale_entries(self):
        aoa_control.register("ctl-6")
        control = aoa_control._CONTROLS["ctl-6"]
        control.created_at = time.monotonic() - (aoa_control._CONTROL_TTL_SECONDS + 10)
        self.assertIsNone(aoa_control.status("ctl-6"))
        # 下一次 request 重新登记（旧的已清理）。
        aoa_control.request("ctl-6", "pause")
        self.assertTrue(aoa_control.status("ctl-6")["paused"])


class PlaybackSequenceControlTests(unittest.TestCase):
    """run_aoa_sequence 控制门接线：预置 cancel 在第一步抛异常。"""

    def test_preset_cancel_fails_sequence_at_first_step(self):
        clock = _Clock()
        aoa_control._CONTROLS.clear()

        # 门控协议即 sequence 每条命令前的调用：预置 cancel 在第一步抛。
        aoa_control.request("seq-1", "cancel")  # 预置：先于注册到达
        with self.assertRaises(AoaPlaybackCancelledError):
            aoa_control.wait_if_paused(
                "seq-1", index=1, total=2, sleep=clock.sleep)

        # 预置 pause：第一步门内等待，模拟另一线程两次 poll 后 resume。
        aoa_control.request("seq-2", "pause")
        original_sleep = clock.sleep

        def resume_midway(seconds: float) -> None:
            original_sleep(seconds)
            if len(clock.sleeps) == 2:
                aoa_control.request("seq-2", "resume")

        clock.sleep = resume_midway
        self.assertGreater(aoa_control.wait_if_paused(
            "seq-2", index=1, total=2, sleep=clock.sleep,
            poll_seconds=0.5), 0)
        clock.sleeps.clear()
        self.assertEqual(aoa_control.wait_if_paused(
            "seq-2", index=2, total=2, sleep=clock.sleep), 0.0)
        self.assertEqual(clock.sleeps, [])
        aoa_control._CONTROLS.clear()


if __name__ == "__main__":
    unittest.main()


def test_stale_pause_cannot_override_newer_resume():
    aoa_control.request("ordered", "resume", revision=2)
    aoa_control.request("ordered", "pause", revision=1)
    assert aoa_control.status("ordered")["paused"] is False
    aoa_control.unregister("ordered")


def test_paused_playback_stops_when_lease_is_revoked():
    aoa_control.request("revoked", "pause")
    checks = []

    def validate():
        checks.append(True)
        if len(checks) > 1:
            raise RuntimeError("lease revoked")

    import pytest
    with pytest.raises(RuntimeError, match="lease revoked"):
        aoa_control.wait_if_paused("revoked", index=2, total=3, sleep=lambda _: None, on_tick=validate)
    aoa_control.unregister("revoked")


def test_pause_resume_does_not_replay_completed_input(monkeypatch):
    from worker_agent import aoa_actions
    from worker_agent.tests.test_aoa_actions import _stall_usb

    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, stall_channels=set())
    phases = []
    control_id = "resume-sequence"

    def event(value):
        phases.append((value["phase"], value["index"]))
        if phases[-1] == ("completed", 1):
            aoa_control.request(control_id, "pause")

    def sleep(seconds):
        if seconds == 0.2:
            aoa_control.request(control_id, "resume")

    result = aoa_actions.run_aoa_sequence("partial-hid-device", ["click 10 10", "click 20 20"],
        sleep=sleep, control_id=control_id, on_event=event)
    assert result["commands_executed"] == 2
    assert phases == [("running", 1), ("completed", 1), ("paused", 2), ("resumed", 2), ("running", 2), ("completed", 2)]
    downs = [data for hid, data in state["events"] if hid == 3 and data[0] == 3]
    assert len(downs) == 2


def test_pause_budget_is_bounded_and_local_requests_are_read_during_wait(monkeypatch):
    from types import SimpleNamespace

    import pytest

    clock = _Clock()
    monkeypatch.setattr(aoa_control, "time", SimpleNamespace(monotonic=lambda: clock.now))
    local = {"action": "pause", "revision": 1}
    reads = []

    def read():
        reads.append(True)
        return local

    try:
        with pytest.raises(RuntimeError, match="pause budget exceeded"):
            aoa_control.wait_if_paused("budget", index=2, total=3, sleep=clock.sleep,
                poll_seconds=10, read_source=read)
        assert len(reads) > 1
    finally:
        aoa_control.unregister("budget")
