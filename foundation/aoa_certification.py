"""Validate reference-device evidence for standard direct HID device classes."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from .aoa_calibration import complete_context, five_point_calibration_points
from .aoa_display import rect, standard_point
from .aoa_hid_mode import hid_registrations


MAX_CERTIFICATE_BYTES = 196608
FAMILY_KEYS = ("model", "release", "kernel", "board", "hardware")
CASE_KEYS = ("fingerprint", "display", "density", "rotation", "display_route", "display_id")


def descriptor_sha256():
    descriptor = dict(hid_registrations("direct"))[3]
    return hashlib.sha256(descriptor).hexdigest()


def bounded_json(value, limit=MAX_CERTIFICATE_BYTES):
    if len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode()) > limit:
        raise ValueError("AOA evidence exceeds its size limit")


def _event_devices(output):
    devices = {}
    for match in re.finditer(r"(?ms)^add device \d+: (/dev/input/event\d+)\s*\n(.*?)(?=^add device |\Z)", output):
        devices[match[1]] = match[2]
    return devices


def classify_direct_input(before, after, reader, context):
    """Require a newly registered direct device, its actual axes and viewport."""
    for value in (before, after, reader):
        if not isinstance(value, str) or len(value) > 262144:
            raise ValueError("invalid bounded InputReader evidence")
    previous = _event_devices(before)
    candidates = []
    for path, block in _event_devices(after).items():
        if previous.get(path) == block or "INPUT_PROP_DIRECT" not in block:
            continue
        name = re.search(r'^\s*name:\s*"([^"\n]+)"', block, re.M)
        axes = {}
        for axis in ("X", "Y"):
            match = re.search(rf"ABS_MT_POSITION_{axis}[^\n]*?min\s+(-?\d+)[^\n]*?max\s+(-?\d+)", block)
            if match:
                axes[axis] = [int(match[1]), int(match[2])]
        if name and axes == {"X": [0, 360], "Y": [0, 640]}:
            candidates.append((path, name[1]))
    if len(candidates) != 1:
        raise ValueError("cannot uniquely identify newly registered direct HID axes")
    path, name = candidates[0]
    blocks = list(re.finditer(r"(?m)^\s{2}Device (\d+): ([^\n]+)\n", reader))
    matching = []
    for index, match in enumerate(blocks):
        if match[2].strip() == name:
            matching.append((int(match[1]), reader[match.end():blocks[index + 1].start() if index + 1 < len(blocks) else len(reader)]))
    if len(matching) != 1:
        raise ValueError("AOA InputReader device identity is ambiguous")
    device_id, block = matching[0]
    sources = re.search(r"Sources:\s*0x([0-9a-fA-F]+)", block)
    if (not sources or int(sources[1], 16) & 0x1002 != 0x1002
            or not re.search(r"DeviceType:\s*(?:TOUCH_SCREEN|touchScreen)\b", block)):
        raise ValueError("registered HID is not an InputReader touchscreen")
    viewport = re.search(r"[^\n]*Viewport[^\n]*uniqueId=['\"]([^'\"]+)['\"][^\n]*", block)
    if not viewport:
        raise ValueError("AOA touchscreen has no verifiable display viewport")
    display_id = re.search(r"displayId\s*[=:]\s*(\d+)", viewport[0])
    frame = re.search(r"logicalFrame\s*[=:]\s*(\[[^\n}]+)", viewport[0])
    numbers = re.findall(r"-?\d+", frame[1])[:4] if frame else []
    if (not display_id or viewport[1] != context.get("display_route") or len(numbers) != 4
            or int(display_id[1]) != context.get("display_id", 0)):
        raise ValueError("AOA HID is associated with another or unknown display")
    logical_frame = list(map(int, numbers))
    rect(logical_frame)
    return {"event_path": path, "input_driver": name, "reader_device_id": device_id,
            "sources": int(sources[1], 16), "input_prop_direct": True,
            "axes": {"X": [0, 360], "Y": [0, 640]}, "viewport": logical_frame,
            "display_id": int(display_id[1]), "display_route": viewport[1]}


def validate_reference_evidence(evidence):
    bounded_json(evidence)
    if not isinstance(evidence, dict) or type(evidence.get("version")) is not int or evidence["version"] != 1:
        raise ValueError("unsupported direct-reference evidence version")
    context = copy.deepcopy(evidence.get("context"))
    if (not complete_context(context) or context["hid_touch_mode"] != "direct"
            or any(not isinstance(context.get(key), str) or not context[key].strip()
                   or context[key].strip().lower() in {"unknown", "none", "null", "未知"} for key in FAMILY_KEYS)
            or type(context.get("display_id")) is not int or context["display_id"] < 0):
        raise ValueError("direct reference requires complete hardware and display identity")
    if evidence.get("descriptor_sha256") != descriptor_sha256():
        raise ValueError("HID descriptor differs from the current direct implementation")
    inputs = classify_direct_input(evidence.get("getevent_before"), evidence.get("getevent_after"),
                                   evidence.get("input_reader"), context)
    samples = evidence.get("samples")
    required = set([*five_point_calibration_points(360, 640), (120, 213), (240, 427)])
    if not isinstance(samples, list) or len(samples) != len(required):
        raise ValueError("direct verification requires all seven independent reference measurements")
    measured = {}
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("invalid direct-reference measurement")
        hid, cooked = sample.get("hid"), sample.get("cooked")
        if (not isinstance(hid, list) or len(hid) != 2 or any(type(n) is not int for n in hid)
                or not isinstance(cooked, list) or len(cooked) != 2
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in cooked)):
            raise ValueError("invalid direct-reference measurement")
        if tuple(hid) in measured:
            raise ValueError("duplicate direct-reference measurement")
        measured[tuple(hid)] = cooked
    if set(measured) != required:
        raise ValueError("direct reference probes do not cover the required region")
    fits = []
    for rotation in range(4):
        errors = [math.dist(standard_point(*hid, context["display"], rotation, inputs["viewport"]), cooked)
                  for hid, cooked in measured.items()]
        if max(errors) <= 8:
            fits.append((rotation, max(errors)))
    if len(fits) != 1:
        raise ValueError("observed HID input is not a uniquely verified standard direct transform")
    serial, port = evidence.get("serial"), evidence.get("usb_port_path")
    if not isinstance(serial, str) or not serial or not isinstance(port, str) or not re.fullmatch(r"\d+-\d+(?:\.\d+)*", port):
        raise ValueError("direct reference requires physical device identity")
    return {"context": context, "input": inputs, "axis_rotation": fits[0][0],
            "max_error_px": fits[0][1], "serial": serial, "usb_port_path": port,
            "evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}


def build_certificate(evidence_list, *, certificate_id, owner_id, label):
    if not isinstance(evidence_list, list) or not 2 <= len(evidence_list) <= 16:
        raise ValueError("class certification requires 2..16 independently verified reference devices")
    cases = [validate_reference_evidence(item) for item in evidence_list]
    if len({case["serial"] for case in cases}) < 2:
        raise ValueError("device-class evidence must include at least two distinct device serials")
    first = cases[0]
    family = {key: first["context"][key] for key in FAMILY_KEYS}
    driver = first["input"]["input_driver"]
    if any(any(case["context"][key] != family[key] for key in FAMILY_KEYS)
           or case["input"]["input_driver"] != driver for case in cases):
        raise ValueError("reference devices do not belong to the same input implementation class")
    certificate = {"version": 1, "id": certificate_id, "owner_id": owner_id, "label": label,
                   "family": family, "input_driver": driver, "descriptor_sha256": descriptor_sha256(),
                   "cases": cases, "evidence": copy.deepcopy(evidence_list)}
    bounded_json(certificate, 524288)
    return certificate


def validate_certificate(certificate):
    if not isinstance(certificate, dict):
        raise ValueError("a standard direct plan requires a device-class certificate")
    bounded_json(certificate, 524288)
    for key in ("id", "owner_id", "label"):
        if not isinstance(certificate.get(key), str) or not 1 <= len(certificate[key]) <= 256:
            raise ValueError("invalid device-class certificate identity")
    rebuilt = build_certificate(certificate.get("evidence"), certificate_id=certificate["id"],
                                owner_id=certificate["owner_id"], label=certificate["label"])
    if rebuilt != certificate:
        raise ValueError("device-class certificate does not match its reference evidence")
    return certificate


def matching_case(certificate, context):
    validate_certificate(certificate)
    if (not complete_context(context) or context.get("hid_touch_mode") != "direct"
            or any(context.get(key) != value for key, value in certificate["family"].items())):
        return None
    candidates = [case for case in certificate["cases"] if all(
        context.get(key) == case["context"].get(key) for key in CASE_KEYS)]
    if not candidates:
        return None
    signatures = {(case["axis_rotation"], tuple(case["input"]["viewport"])) for case in candidates}
    if len(signatures) != 1:
        raise ValueError("conflicting certified display transforms require recertification")
    return {**candidates[0], "max_error_px": max(case["max_error_px"] for case in candidates)}
