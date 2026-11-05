"""Evidence required before coordinate-based, unattended firmware recovery."""
from __future__ import annotations

import copy
import re
from datetime import datetime

from .aoa_calibration import TouchMapping, complete_context, context_matches, convert_commands
from .aoa_profile import parse_profile_commands


def has_touch(commands):
    return any(command.kind in {"click", "longClick", "swipe"}
               for command in parse_profile_commands(commands))


def firmware_compatibility(metadata, commands):
    """An explicit, account-owned hardware validation declaration, never inferred.

    This binds one tested firmware image and environment; it is not device-class
    certification and cannot promote a mapping to direct_standard.

    修订（先关闭强制闸门）：未填写声明时返回 None 放行，Worker 端按未验证
    回放并如实标注；一旦填写了声明，仍严格校验全部字段并冻结绑定。
    """
    if not has_touch(commands):
        return None
    raw = metadata.get("firmware_compatibility") if isinstance(metadata, dict) else None
    if raw is None:
        return None
    declaration = raw
    if (not isinstance(declaration, dict) or type(declaration.get("version")) is not int
            or declaration["version"] != 1):
        raise ValueError("AOA firmware compatibility declaration is malformed; "
                         "complete all fields or remove it from the profile")
    digest = declaration.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("AOA firmware compatibility requires the exact firmware SHA-256")
    if not complete_context(declaration.get("context")):
        raise ValueError("AOA firmware compatibility requires a complete target firmware context")
    for key in ("validated_by", "validated_at", "evidence"):
        value = declaration.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise ValueError(f"AOA firmware compatibility requires {key}")
    try:
        stamp = datetime.fromisoformat(declaration["validated_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("AOA firmware validation time must include a timezone") from exc
    return copy.deepcopy(declaration)


def target_firmware_context(metadata, commands, observed_context):
    declaration = firmware_compatibility(metadata, commands)
    if declaration is None:
        return dict(observed_context or {})
    context = declaration["context"]
    if (not isinstance(observed_context, dict)
            or not observed_context.get("model") or observed_context["model"] != context["model"]):
        raise ValueError("AOA firmware declaration does not match the target device model; collect target inventory first")
    return dict(context)


def require_verified_recovery(commands, options, metadata):
    """Validate both mappings and every requested point before USB or flashing.

    An affine transform maps a swipe segment to a segment. The HID region is
    convex, so checking both endpoints also checks coverage of the whole swipe.

    修订（先关闭强制闸门）：无固件声明时不做已验证标定强制要求，按未验证
    映射回放（Worker 端如实标注 estimated/direct_unverified）；声明存在时
    仍走严格绑定路径。语义回放同样仅在声明存在时做冻结环境绑定。
    """
    if not has_touch(commands):
        return
    declaration = firmware_compatibility(metadata, commands)
    if options.get("aoa_semantic_plan") is not None:
        from .aoa_semantics import has_visual_page, validate_plan

        plan = validate_plan(options["aoa_semantic_plan"], options.get("aoa_direct_certificate"))
        first_input = next((step for command, step in zip(parse_profile_commands(commands), plan["template"]["steps"])
                            if command.kind != "sleep"), None)
        if first_input and not has_visual_page(first_input.get("before")):
            raise ValueError("无 ADB 固件恢复的首个输入必须包含可用外部画面前置条件")
        if declaration is not None:
            if (plan["source_commands"] != commands or not context_matches(plan["context"], declaration["context"])
                    or any(plan["context"].get(key) != declaration["context"].get(key)
                           for key in ("board", "hardware", "locale", "display_id"))):
                raise ValueError("semantic recovery plan does not match the frozen firmware environment")
            options["aoa_firmware_sha256"] = declaration["sha256"]
        return
    if declaration is None:
        return
    if options.get("aoa_mapping_policy") not in {"pointer_calibrated", "direct_calibrated"}:
        raise ValueError("AOA 自动恢复缺少已验证的目标触摸映射（verified target mapping）。"
                         "请在对应目标固件上完成触摸标定并保存实测结果，估算坐标仅用于人工调试。")
    if not options.get("aoa_touch_mapping") or not options.get("aoa_source_touch_mapping"):
        raise ValueError("unattended AOA touch recovery requires verified source and target mappings")
    target = TouchMapping.from_dict(options.get("aoa_touch_mapping"))
    source = TouchMapping.from_dict(options.get("aoa_source_touch_mapping"))
    if (not target.verified or not source.verified
            or source.effective_policy not in {"pointer_calibrated", "direct_calibrated"}
            or target.effective_policy != options["aoa_mapping_policy"]
            or source.hid_touch_mode != target.hid_touch_mode or not complete_context(source.context)
            or not context_matches(target.context, declaration["context"])):
        raise ValueError("AOA recovery mappings do not match the validated target firmware environment")
    convert_commands(commands, source, target)


def require_firmware_digest(declaration, actual_sha256):
    if declaration and declaration["sha256"] != actual_sha256:
        raise ValueError("AOA compatibility firmware SHA-256 differs from the actual image; validate this firmware before flashing")
