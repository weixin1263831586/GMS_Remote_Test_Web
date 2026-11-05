"""State mismatches stop before the next HID input; assertions never retry a click."""
import copy
import json

import pytest

from foundation.aoa_semantics import compile_plan
from tests.aoa_semantic_fixtures import certificate, reference, semantic_template
from worker_agent import aoa_actions, aoa_control
from worker_agent.aoa_observation import ObservationUnavailableError, Observer, capture_binding, capture_fingerprint
from worker_agent.aoa_semantics import resolve_live_command, run_semantic_sequence
from worker_agent.aoa_usb import bind_usb


class Clock:
    value = 0

    def now(self):
        return self.value

    def sleep(self, delay):
        self.value += delay


class Pages:
    def __init__(self, fail=""):
        self.fail = fail

    def evaluate(self, condition, timeout):
        return condition.get("selector", {}).get("text") != self.fail


def options(steps=1):
    commands = ["click 1 1"] * steps
    proof = certificate()
    plan = compile_plan(commands, semantic_template(steps=steps), reference()["context"], proof)
    return {"aoa_semantic_plan": plan, "aoa_direct_certificate": proof, "aoa_hid_touch_mode": "direct"}


@pytest.fixture
def transport(monkeypatch):
    calls = []
    def execute(serial, commands, **kwargs):
        calls.append((serial, commands, kwargs))
        return {"success": True, "native_mode_restored": True}
    monkeypatch.setattr(aoa_actions, "run_aoa_sequence", execute)
    return calls


def test_completion_is_reported_only_after_postcondition(transport):
    opts, phases, clock = options(2), [], Clock()
    result = run_semantic_sequence("TARGET", ["click 1 1"] * 2, opts, observer=Pages(),
        clock=clock.now, sleep=clock.sleep, on_event=lambda event: phases.append(event["phase"]))
    assert len(transport) == 2
    assert phases == ["checking", "running", "verifying", "completed"] * 2
    assert result["mapping_policy"] == "direct_standard"
    assert result["verified_steps"] == 2
    assert all(call[2]["mapping_policy"] == "direct_unverified" for call in transport)


@pytest.mark.parametrize("page,inputs,stage", [("Hi there", 0, "checking"), ("Next", 1, "verifying")])
def test_page_mismatch_halts_without_replaying_input(transport, page, inputs, stage):
    clock = Clock()
    with pytest.raises(aoa_actions.AoaError) as failed:
        run_semantic_sequence("TARGET", ["click 1 1"] * 2, options(2), observer=Pages(page),
                              clock=clock.now, sleep=clock.sleep)
    assert len(transport) == inputs
    assert failed.value.aoa_result["failed_index"] == 1
    assert failed.value.aoa_result["commands_executed"] == 0
    assert failed.value.aoa_result["semantic_stage"] == stage
    assert failed.value.aoa_result["failed_input_state"] == ("uncertain" if inputs else "not_sent")


def test_missing_observer_never_becomes_a_satisfied_assertion(transport):
    class Missing:
        def evaluate(self, condition, timeout):
            raise ObservationUnavailableError("no camera and no ADB")
    clock = Clock()
    with pytest.raises(aoa_actions.AoaError, match="no camera"):
        run_semantic_sequence("TARGET", ["click 1 1"], options(), observer=Missing(), clock=clock.now, sleep=clock.sleep)
    assert transport == []


def test_skip_if_prevents_repeating_an_already_enabled_usb_toggle(transport):
    opts = options()
    template = opts["aoa_semantic_plan"]["template"]
    template["steps"][0]["skip_if"] = {"type": "adb_state", "state": "device"}
    opts["aoa_semantic_plan"] = compile_plan(["click 1 1"], template, reference()["context"], opts["aoa_direct_certificate"])
    result = run_semantic_sequence("TARGET", ["click 1 1"], opts, observer=Pages())
    assert transport == []
    assert result["skipped_steps"] == 1


def test_cancel_and_fencing_loss_stop_before_input(transport):
    opts = {**options(), "playback_control_id": "semantic-cancel"}
    aoa_control.request("semantic-cancel", "cancel")
    with pytest.raises(aoa_actions.AoaError) as failed:
        run_semantic_sequence("TARGET", ["click 1 1"], opts, observer=Pages())
    assert failed.value.cancelled is True
    assert aoa_control.status("semantic-cancel") is None
    def lost():
        raise RuntimeError("lease lost")
    with bind_usb("1-2", lost), pytest.raises(aoa_actions.AoaError, match="lease lost"):
        run_semantic_sequence("TARGET", ["click 1 1"], options(), observer=Pages())
    assert transport == []


def test_pause_resume_keeps_state_assertions_and_input_order(transport):
    opts = {**options(), "playback_control_id": "semantic-pause"}
    aoa_control.request("semantic-pause", "pause")
    clock = Clock()
    def sleep(delay):
        clock.sleep(delay)
        aoa_control.request("semantic-pause", "resume")
    result = run_semantic_sequence("TARGET", ["click 1 1"], opts, observer=Pages(), clock=clock.now, sleep=sleep)
    assert result["success"] is True
    assert len(transport) == 1


def test_usb_cleanup_failure_stops_before_any_postcondition_or_next_step(monkeypatch):
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        return {"success": True, "cleanup_errors": [{"step": "reset_usb", "error": "gone"}]}
    monkeypatch.setattr(aoa_actions, "run_aoa_sequence", execute)
    with pytest.raises(aoa_actions.AoaError) as failed:
        run_semantic_sequence("TARGET", ["click 1 1"] * 2, options(2), observer=Pages())
    assert len(calls) == 1
    assert failed.value.cleanup_errors[0]["step"] == "reset_usb"


def test_live_selector_uses_actual_control_center_and_rejects_ambiguity():
    opts = options()
    layout = copy.deepcopy(opts["aoa_semantic_plan"]["template"]["layouts"][0])
    layout["targets"]["start"]["selector"] = {"resource_id": "button"}
    class Ui:
        select = staticmethod(Observer.select)
        def nodes(self, timeout):
            return [{"resource_id": "button", "bounds": [200, 400, 600, 600]}]
    assert resolve_live_command("click 1 1", {"target": "start"}, layout,
        opts["aoa_direct_certificate"]["cases"][0], reference()["context"], Ui(), 1) == "click 180 250"
    with pytest.raises(ObservationUnavailableError, match="不唯一"):
        Observer.select([{"text": "x"}, {"text": "x"}], {"text": "x"})


def test_missing_adb_cannot_be_used_as_absent_ui_evidence(monkeypatch):
    observer = Observer("TARGET", reference()["context"])
    monkeypatch.setattr(observer, "adb_state", lambda: "unauthorized")
    with pytest.raises(ObservationUnavailableError):
        observer.evaluate({"type": "ui", "selector": {"text": "Next"}, "exists": False})


def test_capture_binding_cannot_be_substituted_by_profile_or_other_device(monkeypatch):
    binding = {"id": "camera-1", "device": "/dev/video0", "width": 800, "height": 1280,
               "context": {key: reference()["context"][key] for key in ("display", "rotation", "display_id", "display_route")}}
    monkeypatch.setenv("GMS_WORKER_AOA_CAPTURE_BINDINGS", json.dumps({"TARGET@1-2": binding}))
    with bind_usb("1-2"):
        assert capture_binding("TARGET", reference()["context"])["id"] == "camera-1"
        with pytest.raises(ObservationUnavailableError):
            capture_binding("OTHER", reference()["context"])
        with pytest.raises(ObservationUnavailableError):
            capture_binding("TARGET", {**reference()["context"], "rotation": 1})


def test_visual_capture_uses_argv_and_bounded_region(monkeypatch):
    from PIL import Image

    from foundation.command_result import CommandResult
    from worker_agent import aoa_observation

    calls = []
    def capture(argv, timeout):
        calls.append(argv)
        Image.new("RGB", (800, 1280), (20, 20, 20)).save(argv[-1])
        return CommandResult(stdout="", stderr="", code=0)
    monkeypatch.setattr(aoa_observation, "run_local_command", capture)
    binding = {"device": "/dev/video0", "width": 800, "height": 1280}
    assert capture_fingerprint(binding, [0.1, 0.2, 0.8, 0.9]) == "14" * 256
    assert calls[0][0] == "ffmpeg" and "-nostdin" in calls[0]


def test_display_changes_between_precondition_and_input_are_rejected(transport):
    class Changed(Pages):
        def verify_context(self):
            raise ObservationUnavailableError("display changed")
    with pytest.raises(aoa_actions.AoaError, match="display changed"):
        run_semantic_sequence("TARGET", ["click 1 1"], options(), observer=Changed())
    assert transport == []


def test_observer_checks_actual_viewport_and_density(monkeypatch):
    from types import SimpleNamespace

    context = reference()["context"]
    observer = Observer("TARGET", context)
    monkeypatch.setattr(observer, "adb_state", lambda: "device")
    def read(serial, *args, **kwargs):
        if "density" in args:
            output = "Physical density: 240"
        elif "size" in args:
            output = "Physical size: 800x1280"
        else:
            output = reference()["input_reader"]
        return SimpleNamespace(stdout=output, returncode=0)
    monkeypatch.setattr(aoa_actions, "_adb_run", read)
    observer.verify_context()
    observer.context = {**context, "rotation": 1}
    with pytest.raises(ObservationUnavailableError):
        observer.verify_context()


def test_capture_capabilities_do_not_publish_device_files(monkeypatch):
    from worker_agent.aoa_observation import capture_capabilities

    binding = {"id": "camera-1", "device": "/dev/video9", "width": 800, "height": 1280,
               "context": {key: reference()["context"][key] for key in ("display", "rotation", "display_id", "display_route")}}
    monkeypatch.setenv("GMS_WORKER_AOA_CAPTURE_BINDINGS", json.dumps({"TARGET@1-2": binding}))
    caps = capture_capabilities()
    assert caps["aoa_capture_bindings"]["TARGET@1-2"] == {"id": "camera-1", "context": binding["context"]}
    assert "/dev/video9" not in json.dumps(caps)


def test_external_display_density_is_never_inherited_from_default():
    from worker_agent.aoa_calibration import enrich_display_densities

    context = {"density": 240, "displays": [{"display_id": 1, "density": None}]}
    enrich_display_densities(context, lambda display_id: "Physical density: 160\nOverride density: 200")
    assert context["density"] == 240
    assert context["displays"][0]["density"] == 200
    enrich_display_densities(context, lambda display_id: "unavailable")
    assert context["displays"][0]["density"] is None
