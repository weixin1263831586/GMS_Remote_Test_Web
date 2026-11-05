"""Synthetic class/semantic evidence; these fixtures are never hardware certificates."""
import copy

from foundation.aoa_calibration import five_point_calibration_points
from foundation.aoa_certification import build_certificate, descriptor_sha256
from foundation.aoa_display import standard_point
from tests.aoa_fixtures import verified_mapping


def reference(serial="REF-A", rotation=0, display_id=0):
    context = {**verified_mapping().context, "board": "test-board", "hardware": "test-hw",
               "locale": "en-US", "display_id": display_id, "rotation": rotation,
               "display_route": f"local:{display_id}"}
    width, height = context["display"]
    viewport = f"Viewport: displayId={display_id}, uniqueId='{context['display_route']}', orientation={rotation}, logicalFrame=[0, 0, {width}, {height}]"
    points = [*five_point_calibration_points(360, 640), (120, 213), (240, 427)]
    return {"version": 1, "serial": serial, "usb_port_path": "1-2", "context": context,
        "descriptor_sha256": descriptor_sha256(), "getevent_before": "",
        "getevent_after": 'add device 1: /dev/input/event9\n  name: "GMS AOA Touch"\n'
            '  ABS_MT_POSITION_X : value 0, min 0, max 360\n  ABS_MT_POSITION_Y : value 0, min 0, max 640\n  INPUT_PROP_DIRECT\n',
        "input_reader": f"  Device 9: GMS AOA Touch\n    Sources: 0x00001002\n    DeviceType: touchScreen\n    {viewport}\n",
        "samples": [{"hid": list(point), "cooked": list(standard_point(*point, context["display"], rotation))} for point in points]}


def certificate(rotation=0, display_id=0):
    return build_certificate([reference("REF-A", rotation, display_id), reference("REF-B", rotation, display_id)],
                             certificate_id="test-cert", owner_id="owner-1", label="Synthetic references")


def semantic_template(context=None, *, steps=1):
    context = copy.deepcopy(context or reference()["context"])
    keys = ("model", "release", "fingerprint", "display", "density", "rotation", "display_route", "display_id", "locale")
    return {"version": 1, "layouts": [{"id": "test-layout", "selector": {key: context[key] for key in keys},
        "targets": {"start": {"point": [0.5, 0.75], "bounds": [0.3, 0.65, 0.7, 0.85]}}}],
        "steps": [{"target": "start", "timeout_seconds": 0.25,
                   "before": {"type": "ui", "selector": {"text": "Hi there"}},
                   "after": {"type": "ui", "selector": {"text": "Next"}}} for _ in range(steps)]}
