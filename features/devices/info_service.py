"""Read verified ADB device information without turning empty results into success."""
from __future__ import annotations

from foundation.error_model import ApiError

from . import runtime
from .manager import device_manager
from .support import get_device_properties_optimized


_FIELDS = {
    "serial_no": "Serial Number", "model": "Model", "android_version": "Android Version",
    "fingerprint": "Fingerprint", "build_type": "Build Type", "build_tags": "Build Tags",
    "build_date": "Build Date", "sdk_version": "SDK Version", "security_patch": "Security Patch",
}


def read_device_info(device_id: str, ssh) -> dict:
    try:
        return _read_device_info(device_id, ssh)
    except ApiError as exc:
        _invalidate_cached_readiness(exc)
        raise


def _invalidate_cached_readiness(error: ApiError) -> None:
    details = error.details or {}
    status = {"absent": "offline", "offline": "offline", "unauthorized": "unauthorized",
              "unknown": "unknown"}.get(details.get("adb_state"))
    state = runtime.global_state
    if not state or not status:
        return
    with state.device_cache_lock:
        cache = state.device_cache
        matches = [item for item in cache.get("devices", [])
                   if isinstance(item, dict) and item.get("device_id") == details.get("device")]
        if matches:
            for item in matches:
                item["status"] = status
            cache["timestamp"] = 0


def _read_device_info(device_id: str, ssh) -> dict:
    base = device_manager.get_device_info(device_id, ssh)
    properties = {label: base.get(key, "未知") for key, label in _FIELDS.items()}
    missing = [label for label, value in properties.items()
               if str(value or "").strip().lower() in {"", "未知", "unknown", "n/a"}]
    if len(missing) == len(_FIELDS):
        raise ApiError.upstream_failure(
            "设备属性读取失败，未获取到有效信息", service="adb",
            details={"device": device_id, "adb_state": "device"},
            next_actions=({"action": "确认 USB 调试可用后重新读取设备信息"},))
    extra = get_device_properties_optimized(device_id, ssh)
    formatters = {
        "boot_state": ("Boot State", lambda value: value or "Unknown"),
        "api_level": ("API Level", lambda value: value.split("[")[-1].replace("]", "")
                      if "[" in value else value or "Unknown"),
        "mali_version": ("Mali Version", lambda value: value or "Unknown"),
        "mem_total": ("Total Memory", lambda value: f"{value} KB" if value else "Unknown"),
        "mem_free": ("Free Memory", lambda value: f"{value} KB" if value else "Unknown"),
        "timezone": ("Timezone", lambda value: value or "Unknown"),
        "locale": ("Language", lambda value: value or "Unknown"),
        "data_partition": ("DATA Partition", lambda value: value.split()[-1]
                           if value and "userdata" in value else "Unknown"),
    }
    for key, (label, formatter) in formatters.items():
        if key in extra:
            properties[label] = formatter(extra[key])
    return {"device": device_id, "success": True, "adb_state": "device", "properties": properties,
            "complete": not missing, "missing_properties": missing}
