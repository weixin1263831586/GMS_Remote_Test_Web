"""Resolve source/target mappings without treating a recording as target evidence."""
from foundation.aoa_calibration import TouchMapping, context_matches, mapping_options

from .aoa_calibrations import calibration_store


def replay_mapping_options(metadata: dict, target_context: dict | None, owner_id: str, mode: str) -> dict:
    calibration = metadata.get("calibration") or {}
    source_raw = calibration.get("mapping") if isinstance(calibration, dict) else None
    source = TouchMapping.from_dict(source_raw) if source_raw is not None else None
    result = {"aoa_hid_touch_mode": mode}
    if source:
        result["aoa_source_touch_mapping"] = source.to_dict()
    context = dict(target_context or {})
    context["hid_touch_mode"] = mode
    complete = (context.get("model") and context.get("release")
                and isinstance(context.get("display"), list)
                and len(context["display"]) == 2 and type(context.get("rotation")) is int)
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
