"""Per-command replay preserves manual USB boundaries without releasing the lease."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from foundation.aoa_profile import parse_profile_commands
from worker_agent import aoa_actions, aoa_control
from worker_agent.aoa_step_sessions import run_step_sessions


def clean_result():
    return {"success": True, "commands_executed": 1, "native_mode_restored": True,
            "cleanup_errors": [], "usb_reconnections": 0}


def execute(commands, **kwargs):
    return run_step_sessions("ABC", commands, parse_profile_commands(commands),
        error_type=RuntimeError, sleep=kwargs.pop("sleep", lambda seconds: None),
        validate=kwargs.pop("validate", lambda: None),
        wait_adb=kwargs.pop("wait_adb", lambda *args: {}), **kwargs)


def test_each_input_has_a_clean_session_sleep_runs_with_hid_absent_and_wait_adb_once():
    commands = ["click 180 525", "sleep 2000", "back"]
    sessions, events, order = [], [], []

    def one(text, deadline, report):
        sessions.append(text)
        order.extend([("connect", text), ("input", text), ("cleanup", text)])
        return clean_result()

    wait = Mock(return_value={"adb_recovered": True, "adb_state": "device"})
    result = execute(commands, run_one=one, on_event=events.append,
        sleep=lambda seconds: order.append(("native_sleep", seconds)), wait_adb=wait, wait_adb_seconds=60)
    assert sessions == ["click 180 525", "back"]
    assert order[2:4] == [("cleanup", "click 180 525"), ("native_sleep", 2)]
    assert [(event["phase"], event["index"], event["total"]) for event in events] == [
        ("running", 1, 3), ("completed", 1, 3), ("running", 2, 3),
        ("completed", 2, 3), ("running", 3, 3), ("completed", 3, 3)]
    assert result["commands_executed"] == 3 and result["success"] is True
    assert result["hid_session_mode"] == "per_command"
    wait.assert_called_once_with("ABC", 60)


def test_uncertain_input_is_not_retried_and_later_steps_never_run():
    one = Mock(side_effect=[clean_result(), RuntimeError("device vanished")])
    with pytest.raises(RuntimeError, match="device vanished"):
        execute(["click 180 525", "click 180 450", "click 290 320"], run_one=one)
    assert one.call_count == 2
    assert [call.args[0] for call in one.call_args_list] == ["click 180 525", "click 180 450"]


def test_nested_failure_receipt_uses_profile_step_instead_of_single_step_index():
    error = RuntimeError("connection vanished before input")
    error.aoa_result = {"failed_index": 1, "failed_input_state": "not_sent", "failed_reports_sent": 0}
    one = Mock(side_effect=[clean_result(), error])
    with pytest.raises(RuntimeError) as caught:
        execute(["click 320 320", "sleep 3000", "click 180 280"], run_one=one)
    result = caught.value.aoa_result
    assert result["failed_index"] == 3 and result["commands_executed"] == 2
    assert result["failed_command"] == "click 180 280"
    assert result["failed_input_state"] == "not_sent"
    assert result["last_input_index"] == 1 and result["last_input_command"] == "click 320 320"


@pytest.mark.parametrize("failure", [{"native_mode_restored": False}, {"cleanup_errors": [{"step": "reset_usb"}]}])
def test_unclean_usb_state_stops_before_next_step(failure):
    one = Mock(return_value={**clean_result(), **failure})
    with pytest.raises(RuntimeError, match="clean USB state"):
        execute(["click 180 525", "back"], run_one=one)
    one.assert_called_once()


def test_cancel_stops_before_next_session_and_unregisters_control():
    one = Mock(return_value=clean_result())
    states = iter([{"action": "resume", "revision": 1}, {"action": "cancel", "revision": 2}])
    with pytest.raises(aoa_control.AoaPlaybackCancelledError):
        execute(["click 180 525", "back"], run_one=one,
            control_id="pb-step-cancel", control_source=lambda: next(states))
    one.assert_called_once()
    assert aoa_control.status("pb-step-cancel") is None


def test_fencing_is_checked_between_sessions():
    lost = False

    def one(*args):
        nonlocal lost
        lost = True
        return clean_result()

    def validate():
        if lost:
            raise RuntimeError("claim changed")

    with pytest.raises(RuntimeError, match="claim changed"):
        execute(["click 180 525", "back"], run_one=one, validate=validate)


def test_public_sequence_uses_single_step_engine_and_preserves_its_mode(monkeypatch):
    from worker_agent.tests.test_aoa_actions import _stall_device_factory, _stall_usb

    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, set())
    core, _ = aoa_actions._usb()
    def find(**kwargs):
        device = _stall_device_factory(state, set())
        transfer = device.ctrl_transfer
        def request(request_type, request, value, index, data, timeout):
            if request == 53:  # Accessory START re-enumerates after each native reset.
                state["reset"] = 0
            return transfer(request_type, request, value, index, data, timeout)
        device.ctrl_transfer = request
        return iter([device])
    monkeypatch.setattr(core, "find", find)
    resets = []
    reset = aoa_actions._reset_usb
    monkeypatch.setattr(aoa_actions, "_reset_usb", lambda device: (resets.append(device), reset(device))[-1])
    monkeypatch.setattr(aoa_actions, "_usb", lambda: (core, SimpleNamespace(dispose_resources=lambda device: None)))
    result = aoa_actions.run_aoa_sequence("partial-hid-device", ["click 180 525", "sleep 200", "back"],
        session_mode="per_command", touch_mode="pointer", sleep=lambda seconds: None)
    assert state["registered"] == [3, 2]  # A touch-only session, then a system-key-only session.
    assert len(resets) == 2
    assert result["commands_executed"] == 3 and result["hid_session_mode"] == "per_command"


def test_pause_resume_keeps_global_step_number_and_never_redispatches(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("worker_agent.aoa_step_sessions.time.monotonic", lambda: clock[0])
    state = {"action": "pause", "revision": 1}
    events = []

    def sleep(seconds):
        clock[0] += seconds
        state.update(action="resume", revision=2)

    one = Mock(return_value=clean_result())
    result = execute(["click 180 525", "back"], run_one=one, on_event=events.append,
        sleep=sleep, control_id="pb-step-pause", control_source=lambda: state)
    assert result["success"] is True and one.call_count == 2
    assert [(e["phase"], e["index"]) for e in events][:3] == [("paused", 1), ("resumed", 1), ("running", 1)]


def test_operation_budget_is_checked_before_any_input(monkeypatch):
    ticks = iter([0.0, 1000.0])
    monkeypatch.setattr("worker_agent.aoa_step_sessions.time.monotonic", lambda: next(ticks))
    one = Mock()
    with pytest.raises(RuntimeError, match="budget exceeded"):
        execute(["click 180 525"], run_one=one)
    one.assert_not_called()
