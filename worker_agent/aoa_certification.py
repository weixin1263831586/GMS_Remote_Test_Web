"""Collect class-reference evidence only on the dedicated, focused safe surface."""
import re
import time

from foundation.aoa_certification import classify_direct_input, descriptor_sha256, validate_reference_evidence

from .aoa_usb import bound_port, validate_binding


def verify_direct_reference(serial, display_id=0):
    from . import aoa_actions as actions
    from .aoa_calibration import display_context

    width, height = actions._display_size(serial)
    context = display_context(serial, width, height, actions._adb_run, "direct", display_id)
    width, height = context["display"]
    before = actions._adb_run(serial, "shell", "getevent", "-lp", timeout=15)
    if before.returncode:
        raise actions.AoaError("无法读取参考设备的实际输入轴和分类")
    captured = {}

    def collect():
        validate_binding()
        event = actions._adb_run(serial, "shell", "getevent", "-lp", timeout=15)
        reader = actions._adb_run(serial, "shell", "dumpsys", "input", timeout=15)
        if event.returncode or reader.returncode:
            raise actions.AoaError("无法验证新注册的 AOA 输入设备")
        inputs = classify_direct_input(before.stdout, event.stdout, reader.stdout, context)
        captured.update({"getevent_after": event.stdout, "input_reader": reader.stdout, "input": inputs})

    def probe(dump, x, y):
        device_id = captured["input"]["reader_device_id"]
        match = re.search(rf"(?ms)^  Device {re.escape(str(device_id))}:.*?(?=^  Device \d+:|\Z)", dump)
        if match is None:
            return None
        return actions._parse_probe_cooked_point(match[0], x, y)

    mapping = actions._calibrate_touch_affine(serial, width, height, time.sleep, "direct", context=context,
        evidence_hook=collect, probe_reader=probe)
    if mapping is None or not mapping.verified or actions.cleanup_diagnostics():
        raise actions.AoaError("标准 direct 参考验证失败；未生成认证证据，请核对分类、显示路由与落点")
    evidence = {"version": 1, "serial": serial, "usb_port_path": bound_port(), "context": context,
                "descriptor_sha256": descriptor_sha256(), "getevent_before": before.stdout,
                "getevent_after": captured["getevent_after"], "input_reader": captured["input_reader"],
                "samples": list(mapping.samples)}
    validate_reference_evidence(evidence)
    return {"success": True, "serial": serial, "direct_reference_evidence": evidence,
            "class_certified": False, "detail": "参考设备验证通过；类别认证还需另一台独立参考设备"}
