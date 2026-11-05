"""Resolve source/target mappings without treating a recording as target evidence."""
from foundation.aoa_calibration import TouchMapping, complete_context, context_matches, mapping_options
from foundation.cluster_port import get_cluster_service
from foundation.error_model import ApiError

from .aoa_calibrations import calibration_store


def trusted_target_context(worker_id: str, device: str) -> dict:
    try:
        repository = get_cluster_service().repository
    except RuntimeError as exc:
        raise ApiError.dependency_unavailable("设备清单服务不可用，无法核对目标环境") from exc
    target = repository.resolve_worker_device(worker_id, device)
    if target is None:
        raise ApiError.not_found("目标设备不在所选 Worker 的设备清单中")
    context = (target.get("properties") or {}).get("aoa_context")
    return dict(context) if isinstance(context, dict) else {}


def replay_mapping_options(metadata: dict, target_context: dict | None, owner_id: str, mode: str, commands=None) -> dict:
    if metadata.get("semantic_template") is not None:
        from .aoa_semantic_service import semantic_options

        if mode != "direct" or commands is None:
            raise ValueError("semantic templates require explicit direct mode and their reviewed command list")
        return mapping_options({"aoa_hid_touch_mode": mode,
            **semantic_options(metadata, target_context or {}, owner_id, commands)})
    calibration = metadata.get("calibration") or {}
    source_raw = calibration.get("mapping") if isinstance(calibration, dict) else None
    source = TouchMapping.from_dict(source_raw) if source_raw is not None else None
    result = {"aoa_hid_touch_mode": mode}
    if source:
        result["aoa_source_touch_mapping"] = source.to_dict()
    context = dict(target_context or {})
    context["hid_touch_mode"] = mode
    complete = complete_context(context)
    record = calibration_store().find_matching(owner_id, context) if complete else None
    if record:
        result["aoa_touch_mapping"] = record["mapping"]
        result["aoa_mapping_policy"] = record["mapping_policy"]
    elif source and complete and source.verified and context_matches(source.context, context):
        result["aoa_touch_mapping"] = source.to_dict()
        result["aoa_mapping_policy"] = source.effective_policy
    elif source:
        result["aoa_mapping_policy"] = "unsupported"
    else:
        result["aoa_mapping_policy"] = metadata.get("mapping_policy") or (
            "direct_unverified" if mode == "direct" else "estimated")
    return mapping_options(result)
