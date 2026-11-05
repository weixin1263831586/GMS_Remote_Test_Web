"""Bounded semantic layouts, predicates and certified display-aware plans."""
from __future__ import annotations

import copy
import math
import re

from .aoa_certification import bounded_json, matching_case
from .aoa_display import rect, standard_inverse, standard_point, target_point
from .aoa_profile import parse_profile_commands


IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
SELECTOR_KEYS = {"text", "resource_id", "content_desc", "checked", "enabled"}
LAYOUT_KEYS = {"model", "release", "fingerprint", "display", "density", "rotation", "locale", "display_route", "display_id", "board", "hardware", "kernel"}


def validate_selector(selector):
    if (not isinstance(selector, dict) or not selector or set(selector) - SELECTOR_KEYS
            or not any(key in selector for key in ("text", "resource_id", "content_desc"))):
        raise ValueError("UI selector requires an exact text, resource ID or content description")
    for key, value in selector.items():
        if key in {"checked", "enabled"}:
            if type(value) is not bool:
                raise ValueError("UI state selector must be boolean")
        elif not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError("UI selector string must be 1..256 characters")


def validate_condition(condition, depth=0):
    if not isinstance(condition, dict) or depth > 3:
        raise ValueError("invalid bounded semantic condition")
    kind = condition.get("type")
    if kind in {"all", "any"}:
        terms = condition.get("conditions")
        if not isinstance(terms, list) or not 1 <= len(terms) <= 8:
            raise ValueError("semantic condition requires 1..8 bounded terms")
        for term in terms:
            validate_condition(term, depth + 1)
    elif kind == "ui":
        validate_selector(condition.get("selector"))
        if type(condition.get("exists", True)) is not bool:
            raise ValueError("UI existence condition must be boolean")
    elif kind == "adb_state":
        if condition.get("state") not in ("device", "unauthorized", "offline", "absent"):
            raise ValueError("invalid expected ADB state")
    elif kind == "visual":
        rect(condition.get("roi"), normalized=True)
        if not isinstance(condition.get("fingerprint"), str) or not re.fullmatch(r"[a-f0-9]{512}", condition["fingerprint"]):
            raise ValueError("visual condition requires a captured 16x16 grayscale reference")
        if type(condition.get("max_delta", 3)) is not int or not 0 <= condition.get("max_delta", 3) <= 12:
            raise ValueError("visual tolerance must be 0..12 grayscale levels")
        if not isinstance(condition.get("binding_id"), str) or not IDENTIFIER.fullmatch(condition["binding_id"]):
            raise ValueError("visual condition requires a trusted capture binding ID")
    else:
        raise ValueError("unknown semantic condition type; blind fallback is forbidden")


def has_page_condition(condition):
    """Every satisfying branch must identify an actual UI page."""
    if not condition:
        return False
    if condition.get("type") in {"ui", "visual"}:
        return condition.get("exists", True) is True
    terms = condition.get("conditions", [])
    if condition.get("type") == "all":
        return any(has_page_condition(term) for term in terms)
    if condition.get("type") == "any":
        return bool(terms) and all(has_page_condition(term) for term in terms)
    return False


def has_visual_page(condition):
    if not condition:
        return False
    kind = condition.get("type")
    if kind == "visual":
        return True
    terms = condition.get("conditions", [])
    if kind == "any":
        return any(has_visual_page(term) for term in terms)
    if kind == "all":
        return (any(has_visual_page(term) for term in terms)
                and all((term.get("type") == "adb_state" and term.get("state") != "device")
                        or has_visual_page(term) for term in terms))
    return False


def require_capture_capability(options, capabilities, serial, port):
    plan = options.get("aoa_semantic_plan")
    if plan is None:
        return
    capabilities = capabilities or {}
    bindings = capabilities.get("aoa_capture_bindings") or {}
    if not isinstance(bindings, dict):
        raise ValueError("Worker 外部画面绑定清单无效，请刷新 Worker")
    binding = bindings.get(f"{serial.split(':', 1)[-1]}@{port}")
    if (capabilities.get("aoa_visual_capture_v1") is not True or not isinstance(binding, dict)
            or not isinstance(binding.get("context"), dict)
            or any(binding.get("context", {}).get(key) != plan["context"].get(key)
                   for key in ("display", "rotation", "display_route", "display_id"))):
        raise ValueError("语义固件恢复需要目标设备和显示器已绑定的外部画面源，请配置并刷新 Worker")

    def check(condition):
        if not condition:
            return
        if condition.get("type") == "visual" and condition["binding_id"] != binding.get("id"):
            raise ValueError("语义模板的画面参考与目标设备绑定不一致")
        for child in condition.get("conditions", []):
            check(child)
    for step in plan["template"]["steps"]:
        for field in ("before", "after", "skip_if"):
            check(step.get(field))


def validate_template(template, commands):
    bounded_json(template, 65536)
    parsed = parse_profile_commands(commands)
    if not isinstance(template, dict) or type(template.get("version")) is not int or template["version"] != 1:
        raise ValueError("unsupported semantic template version")
    layouts, steps = template.get("layouts"), template.get("steps")
    if not isinstance(layouts, list) or not 1 <= len(layouts) <= 16:
        raise ValueError("semantic template requires 1..16 explicit layouts")
    if not isinstance(steps, list) or len(steps) != len(parsed):
        raise ValueError("semantic steps must correspond one-to-one with the visible command list")
    ids = []
    for layout in layouts:
        if not isinstance(layout, dict) or not isinstance(layout.get("id"), str) or not IDENTIFIER.fullmatch(layout["id"]):
            raise ValueError("semantic layout requires a stable ID")
        ids.append(layout["id"])
        selector = layout.get("selector")
        required = {"model", "release", "fingerprint", "locale", "display", "density", "rotation", "display_route", "display_id"}
        if not isinstance(selector, dict) or required - set(selector) or set(selector) - LAYOUT_KEYS:
            raise ValueError("semantic layout requires explicit firmware, language and display selectors")
        if any(selector.get(key) in (None, "") for key in required):
            raise ValueError("unknown layout identity cannot select a UI template")
        rect(layout.get("frame", [0, 0, 1, 1]), normalized=True)
        if type(layout.get("rotate_quarters", 0)) is not int or layout.get("rotate_quarters", 0) not in range(4):
            raise ValueError("layout rotation must be an explicit 0..3 quarter turn")
        targets = layout.get("targets")
        if not isinstance(targets, dict) or not 1 <= len(targets) <= 128:
            raise ValueError("semantic layout requires 1..128 named targets")
        for name, target in targets.items():
            if not IDENTIFIER.fullmatch(name) or not isinstance(target, dict):
                raise ValueError("invalid semantic target")
            target_point(target, layout.get("frame", [0, 0, 1, 1]), [1000, 1000])
            if target.get("selector"):
                validate_selector(target["selector"])
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate semantic layout ID")
    budget = 0
    for index, (step, command) in enumerate(zip(steps, parsed)):
        if not isinstance(step, dict):
            raise ValueError("semantic step must be an object")
        if type(step.get("timeout_seconds", 3)) not in (int, float) or not 0.25 <= step.get("timeout_seconds", 3) <= 30:
            raise ValueError("semantic condition timeout must be 0.25..30 seconds")
        budget += 2 * step.get("timeout_seconds", 3)
        for field in ("before", "after", "skip_if"):
            if step.get(field) is not None:
                validate_condition(step[field])
        if command.kind != "sleep" and (not step.get("before") or not step.get("after")):
            raise ValueError(f"semantic step {index + 1} requires both preconditions and postconditions")
        if command.kind != "sleep" and not has_page_condition(step["before"]):
            raise ValueError("semantic input precondition must identify the visible page")
        names = [step.get("target")] if command.kind in {"click", "longClick"} else (
            [step.get("from_target"), step.get("to_target")] if command.kind == "swipe" else [])
        if any(not isinstance(name, str) or name not in layout["targets"] for name in names for layout in layouts):
            raise ValueError("semantic target is missing from one or more layouts")
    if not math.isfinite(budget) or budget > 600:
        raise ValueError("semantic verification budget exceeds 600 seconds")
    return template


def compile_plan(commands, template, context, certificate):
    validate_template(template, commands)
    case = matching_case(certificate, context)
    if case is None:
        raise ValueError("device class or display configuration has no active verified direct evidence")
    layouts = [layout for layout in template["layouts"] if all(
        context.get(key) == value for key, value in layout["selector"].items())]
    if len(layouts) != 1:
        raise ValueError("semantic UI layout is unknown or ambiguous; automatic selection refused")
    layout = layouts[0]
    frame = layout.get("frame", [0, 0, 1, 1])
    compiled = []

    def point(name):
        px, py = target_point(layout["targets"][name], frame, context["display"],
            rotate_quarters=layout.get("rotate_quarters", 0), max_error_px=case["max_error_px"])
        hid = standard_inverse(px, py, context["display"], case["axis_rotation"], case["input"]["viewport"])
        cooked = standard_point(*hid, context["display"], case["axis_rotation"], case["input"]["viewport"])
        target_point(layout["targets"][name], frame, context["display"],
            rotate_quarters=layout.get("rotate_quarters", 0),
            max_error_px=case["max_error_px"] + math.dist((px, py), cooked))
        return hid

    for raw, command, step in zip(commands, parse_profile_commands(commands), template["steps"]):
        if command.kind in {"click", "longClick"}:
            u, v = point(step["target"])
            compiled.append(f"{command.kind} {u} {v}")
        elif command.kind == "swipe":
            u1, v1 = point(step["from_target"])
            u2, v2 = point(step["to_target"])
            compiled.append(f"swipe {u1} {v1} {command.duration_ms} {u2} {v2}")
        else:
            compiled.append(raw)
    parse_profile_commands(compiled)
    return {"version": 1, "source_commands": list(commands), "commands": compiled,
            "template": copy.deepcopy(template), "context": copy.deepcopy(context),
            "certificate_id": certificate["id"], "layout_id": layout["id"]}


def validate_plan(plan, certificate):
    bounded_json(plan, 131072)
    if not isinstance(plan, dict):
        raise ValueError("certified semantic plan is required")
    expected = compile_plan(plan.get("source_commands"), plan.get("template"), plan.get("context"), certificate)
    if expected != plan:
        raise ValueError("semantic plan differs from its reviewed layout and reference evidence")
    return plan
