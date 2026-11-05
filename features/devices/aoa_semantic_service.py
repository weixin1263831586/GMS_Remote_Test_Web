"""Resolve owned templates and authenticate class proofs before device claims."""
from foundation.aoa_certification import FAMILY_KEYS
from foundation.aoa_display import display_contexts
from foundation.aoa_semantics import compile_plan, validate_plan
from foundation.error_model import ApiError

from .aoa_certifications import certification_store


def semantic_options(metadata, context, owner_id, commands):
    template = metadata.get("semantic_template")
    ids = metadata.get("class_certificate_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 16 or any(not isinstance(value, str) for value in ids):
        raise ValueError("semantic templates require explicit owned class certificate IDs")
    display_id = metadata.get("target_display_id")
    if display_id is not None and (type(display_id) is not int or not 0 <= display_id <= 255):
        raise ValueError("target display ID must be 0..255")
    candidates = []
    for certificate_id in ids:
        certificate = certification_store().get(certificate_id, owner_id)
        for display in display_contexts(context):
            if display_id is not None and display.get("display_id") != display_id:
                continue
            try:
                plan = compile_plan(commands, template, {**display, "hid_touch_mode": "direct"}, certificate)
            except ValueError:
                continue
            candidates.append((plan, certificate))
    if len(candidates) != 1:
        raise ValueError("no unique certified semantic layout matches this firmware, language and display")
    plan, certificate = candidates[0]
    return {"aoa_semantic_plan": plan, "aoa_direct_certificate": certificate, "aoa_mapping_policy": "direct_standard"}


def authorize_semantic_options(options, owner_id, observed_context, repository, worker_id, automation_run_id=""):
    plan = options.get("aoa_semantic_plan")
    if plan is None:
        return options
    certificate = certification_store().get(plan["certificate_id"], owner_id)
    if certificate != options.get("aoa_direct_certificate"):
        raise ApiError.conflict("类别认证证据已改变，请重新解析模板")
    try:
        validate_plan(plan, certificate)
    except (ValueError, TypeError) as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    context = plan["context"]
    observed = {**(observed_context or {}), "hid_touch_mode": "direct"}
    observed = next((item for item in display_contexts(observed) if item.get("display_id") == context.get("display_id")), observed)
    if automation_run_id:
        command = repository.find_correlated_command(worker_id, "flash_firmware", "automation_run_id", automation_run_id) or {}
        receipt_sha256 = str((command.get("payload") or {}).get("sha256") or "")
        if (command.get("status") != "completed" or (command.get("result") or {}).get("success") is not True
                or not receipt_sha256):
            raise ApiError.conflict("语义恢复尚无成功烧写回执，不能派发认证语义回放")
        # 实际烧写摘要（flash 回执 payload.sha256）是权威绑定；固件兼容性
        # 声明是可选的事前证据（ADR 0028 Revision 1）。两者不得混为一谈：
        # 声明存在时必须与回执一致，无声明时回执本身就构成恢复证明。
        declared_sha256 = str(options.get("aoa_firmware_sha256") or "")
        if declared_sha256 and declared_sha256 != receipt_sha256:
            raise ApiError.conflict("语义恢复固件声明摘要与实际烧写镜像不一致")
        # 刷机后 ADB 关闭（usb_only 清单只报 usb/VID/PID），model/board/
        # hardware 只能来自烧写前冻结进认证计划的 ADB 环境，不得要求
        # 清单重新上报。这里只校验清单仍在场报告的键：报告值与冻结
        # 计划冲突仍拒绝，物理身份（serial/USB 端口/租约）由派发链路
        # 与 Worker 端 validate_binding 继续把关。
        keys = tuple(key for key in ("model", "board", "hardware")
                     if observed.get(key) not in (None, ""))
    else:
        keys = (*FAMILY_KEYS, "fingerprint", "display", "density", "rotation", "display_route", "display_id", "locale")
    if any((not observed.get(key) and observed.get(key) != 0) or observed.get(key) != context.get(key) for key in keys):
        raise ApiError.conflict("语义计划与实际目标设备环境不一致，请刷新设备清单")
    return {**options, "aoa_direct_certificate": certificate}


def validate_owned_semantic(options, owner_id):
    plan = options.get("aoa_semantic_plan")
    if plan is not None:
        certificate = certification_store().get(plan["certificate_id"], owner_id)
        if certificate != options.get("aoa_direct_certificate"):
            raise ApiError.conflict("类别认证已改变，请重新创建恢复任务")
        validate_plan(plan, certificate)
    return options
