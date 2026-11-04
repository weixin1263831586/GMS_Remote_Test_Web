"""Persistent single-step input must retain HID between explicit user commands."""
from types import SimpleNamespace

import pytest

from foundation.aoa_profile import parse_profile_commands
from worker_agent import aoa_actions, aoa_control
from worker_agent.aoa_debug_session import debug_steps
from worker_agent.tests.test_aoa_actions import _stall_usb


def test_public_sequence_registers_full_channels_once_and_only_executes_queued_inputs(monkeypatch):
    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, set())
    core, _ = aoa_actions._usb()
    monkeypatch.setattr(aoa_actions, "_usb", lambda: (core, SimpleNamespace(dispose_resources=lambda device: None)))
    clock, events, reads = [0.0], [], [0]
    monkeypatch.setattr(aoa_actions.time, "monotonic", lambda: clock[0])

    def source(*, close=False):
        assert not state.get("reset")  # USB must remain open, including idle polls.
        reads[0] += 1
        if reads[0] == 1:
            return {"sequence": 1, "command": "click 180 525"}
        if reads[0] == 4:
            return {"sequence": 2, "command": "click 180 580"}
        if close:
            assert reads[0] > 4
        return None

    result = aoa_actions.run_aoa_sequence("partial-hid-device", ["click 180 525", "back", "write a"],
        debug_command_source=source, on_event=events.append, sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert result["commands_executed"] == 2
    assert state["registered"] == [3, 1, 2]  # Frozen from full batch, including unexecuted commands.
    assert state["reset"] == 1 and result["native_mode_restored"] is True
    assert [(event["phase"], event["debug_sequence"]) for event in events] == [
        ("running", 1), ("completed", 1), ("running", 2), ("completed", 2)]
    assert all(channel == 3 for channel, data in state["events"][:-3])  # Only requested touch inputs, then cleanup.


def test_idle_debug_cancel_cleans_up_without_sending_next_input(monkeypatch):
    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, set())
    core, _ = aoa_actions._usb()
    monkeypatch.setattr(aoa_actions, "_usb", lambda: (core, SimpleNamespace(dispose_resources=lambda device: None)))
    cancelled = [False]

    def source(**kwargs):
        cancelled[0] = True
        return None

    with pytest.raises(aoa_control.AoaPlaybackCancelledError):
        aoa_actions.run_aoa_sequence("partial-hid-device", ["click 180 525"],
            debug_command_source=source, control_id="dbg-cancel", sleep=lambda seconds: None,
            control_source=lambda: {"action": "cancel" if cancelled[0] else "resume", "revision": int(cancelled[0])})
    assert state["reset"] == 1
    assert aoa_control.status("dbg-cancel") is None
    assert len(state["events"]) == 1  # Release touch only; no user input.


def test_new_hid_channel_is_rejected_before_any_input():
    steps = debug_steps(lambda **kwargs: {"sequence": 1, "command": "write abc"},
        parse_profile_commands(["click 180 525"]), tick=lambda: None, sleep=lambda seconds: None,
        clock=lambda: 0, error_type=RuntimeError)
    with pytest.raises(RuntimeError, match="another HID channel"):
        next(steps)


def test_fencing_is_checked_while_waiting_for_user():
    def lost():
        raise RuntimeError("claim changed")
    steps = debug_steps(lambda **kwargs: None, parse_profile_commands(["click 180 525"]),
        tick=lost, sleep=lambda seconds: None, clock=lambda: 0, error_type=RuntimeError)
    with pytest.raises(RuntimeError, match="claim changed"):
        next(steps)
