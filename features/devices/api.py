"""Devices router - device management APIs."""

import asyncio
import logging
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from . import reconnect, runtime
from .bootloader_api import (
    check_bootloader_status as check_bootloader_status,
)
from .bootloader_api import (
    get_device_info as get_device_info,
)
from .bootloader_api import (
    lock_bootloader as lock_bootloader,
)
from .bootloader_api import (
    router as bootloader_router,
)
from .bootloader_api import (
    unlock_bootloader as unlock_bootloader,
)
from .locks import device_lock_manager
from .management_api import _known_usbip_sources, _prune_inactive_usbip_sources
from .manager import device_manager
from .operations_api import (
    connect_wifi as connect_wifi,
)
from .operations_api import (
    reboot_devices as reboot_devices,
)
from .operations_api import (
    router as operations_router,
)
from .screens_api import show_device_screens as show_device_screens
from .support import SSHConnection, get_or_create_user_state
from .ui_control_api import router as ui_control_router


logger = logging.getLogger(__name__)

router = APIRouter()


def _help_or_continue(help: bool, method: str, path: str):
    if runtime.generate_help_or_continue is None:
        return None
    return runtime.generate_help_or_continue(help, method, path)


def _api_success(data=None, message="操作成功"):
    return success_response(data=data, message=message)


def _api_error(message, status_code=500, **extra_fields):
    return error_response(message, status_code=status_code, **extra_fields)


def _device_results(results, operation_name):
    success_count = sum(result.get("success", False) for result in results)
    failed_count = len(results) - success_count
    return _api_success(
        {
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": failed_count,
            },
        },
        f"{operation_name}完成: 成功 {success_count} 台, 失败 {failed_count} 台",
    )


def _known_usbip_device_ids() -> set:
    """Return USB/IP device ids from in-memory and persisted runtime state."""
    device_ids = set()
    with runtime.global_state.usbip_devices_source_lock:
        device_ids.update(runtime.global_state.usbip_devices_source.keys())
    runtime_sources = (runtime.config_manager.get_runtime_config() or {}).get("usbip_devices_source") or {}
    if isinstance(runtime_sources, dict):
        device_ids.update(str(device_id) for device_id in runtime_sources if device_id)
    return device_ids




@router.get("/api/devices/list")
@handle_api_errors
async def get_connected_devices(
    request: Request,
    help: Annotated[bool, Query()] = False,
    force_refresh: Annotated[bool, Query()] = False,
    source: Annotated[str, Query()] = "auto",
):
    """Get all connected device list (same as adb devices)."""
    resp = _help_or_continue(help, "GET", "/api/devices/list")
    if resp:
        return resp

    if force_refresh:
        logger.info("设备列表刷新: source=%s, force_refresh=%s", source, force_refresh)

    # Track user access
    client_id = runtime.get_client_id_from_request(request)
    get_or_create_user_state(client_id)

    now = datetime.now().timestamp()
    with runtime.global_state.device_cache_lock:
        cached_devices = runtime.global_state.device_cache.get("devices") or []
        cache_timestamp = runtime.global_state.device_cache.get("timestamp", 0)
    cached_inventory = []
    for item in cached_devices:
        if not isinstance(item, dict) or not item.get("device_id"):
            continue
        cached_inventory.append({
            key: value
            for key, value in {
                "device_id": item["device_id"],
                "status": item.get("status") or "online",
                "protocol": item.get("protocol") or (
                    "fastboot" if item.get("status") == "fastboot" else "adb"
                ),
                "transport": item.get("transport") or "local_usb",
                "adb_proxy_source_worker_id": item.get(
                    "adb_proxy_source_worker_id"
                ),
                "adb_proxy_source_address": item.get(
                    "adb_proxy_source_address"
                ),
                "adb_proxy_source_serial": item.get("adb_proxy_source_serial"),
            }.items()
            if value not in {None, ""}
        })
    cached_ids = [item["device_id"] for item in cached_inventory]
    cache_fresh = bool(
        cached_ids
        and not force_refresh
        and now - cache_timestamp < runtime.device_cache_ttl
    )

    # 共享缓存仅保存主机和设备事实，用户状态按请求合并。
    if cache_fresh:
        inventory = cached_inventory
    else:
        adb_devices = await asyncio.to_thread(
            device_manager.get_connected_devices,
            force_refresh,
        )
        fastboot_devices = await asyncio.to_thread(
            device_manager.get_fastboot_devices,
        )
        if adb_devices:
            try:
                from worker_agent.adb_proxy import imported_device_for_serial
            except ImportError:
                imported_device_for_serial = None
            inventory = []
            for device_id in adb_devices:
                proxy_source = (
                    imported_device_for_serial(device_id)
                    if imported_device_for_serial
                    else None
                )
                observed = {
                    "device_id": device_id,
                    "status": "online",
                    "protocol": "adb",
                    "transport": "adb_proxy" if proxy_source else "local_usb",
                }
                if proxy_source:
                    observed.update({
                        "adb_proxy_source_worker_id": proxy_source[
                            "source_worker_id"
                        ],
                        "adb_proxy_source_address": proxy_source[
                            "source_address"
                        ],
                        "adb_proxy_source_serial": proxy_source["source_serial"],
                    })
                inventory.append(observed)
        elif cached_inventory:
            logger.warning("[Device] ADB scan returned no devices; keeping cached device list")
            inventory = list(cached_inventory)
        else:
            inventory = []

        # 同一序列号从 ADB 切到 Fastboot 时以本次 Fastboot 状态为准。
        inventory_by_id = {item["device_id"]: item for item in inventory}
        for device_id in fastboot_devices:
            inventory_by_id[device_id] = {
                "device_id": device_id,
                "status": "fastboot",
                "protocol": "fastboot",
                "transport": "local_usb",
            }
        inventory = list(inventory_by_id.values())

    adb_devices = [
        item["device_id"]
        for item in inventory
        if item.get("protocol") == "adb" and item.get("status") == "online"
    ]
    reconnect.reconcile_observed_usbip_devices(adb_devices)
    visible_ids = reconnect.filter_suppressed_usbip_devices(
        item["device_id"] for item in inventory
    )
    inventory_by_id = {item["device_id"]: item for item in inventory}
    devices = [device_id for device_id in visible_ids if device_id in inventory_by_id]

    # 保留断线设备来源，供 USB/IP 自动重连使用。
    devices_with_status = []
    cache_records = []

    # 设备 -> 所属分组 id 列表（按当前用户读取其 per-user 分组；局部 import 规避循环依赖）
    from features.users import (
        build_device_group_map,
        current_username_for_request,
        load_device_groups,
    )
    group_map = build_device_group_map(
        load_device_groups(current_username_for_request(request))
    )

    usbip_sources = _prune_inactive_usbip_sources(
        devices,
        _known_usbip_sources(),
        runtime.config_manager.load_config()
        if hasattr(runtime.config_manager, "load_config")
        else {},
    )

    for device_id in devices:
        observed = inventory_by_id[device_id]
        device_info = {
            "device_id": device_id,
            "status": observed.get("status") or "online",
            "protocol": observed.get("protocol") or "adb",
            "transport": observed.get("transport") or "local_usb",
            "locked": False,
        }
        for key in (
            "adb_proxy_source_worker_id",
            "adb_proxy_source_address",
            "adb_proxy_source_serial",
        ):
            if observed.get(key):
                device_info[key] = observed[key]

        # 所属分组（仅内存查表，不增加网络往返）
        device_info["groups"] = group_map.get(device_id, [])

        # Check lock status
        lock_status = device_lock_manager.get_lock_status(device_id)

        if lock_status:
            device_info["locked"] = True
            device_info["locked_by"] = lock_status["locked_by"]
            device_info["locked_username"] = lock_status.get("username", "")
            device_info["locked_client_id"] = lock_status.get("client_id", "")
            device_info["locked_by_self"] = lock_status.get("client_id") == client_id
            device_info["locked_at"] = lock_status["locked_at"]
        else:
            device_info["locked_by"] = ""
            device_info["locked_username"] = ""
            device_info["locked_client_id"] = ""
            device_info["locked_by_self"] = False

        # Check USB/IP source
        if device_id in usbip_sources:
            source = usbip_sources[device_id]
            device_info["source"] = source.get("source", "")
            device_info["is_usbip"] = True

        devices_with_status.append(device_info)
        cache_records.append(
            {
                key: device_info[key]
                for key in (
                    "device_id", "status", "protocol", "transport",
                    "source", "is_usbip", "adb_proxy_source_worker_id",
                    "adb_proxy_source_address", "adb_proxy_source_serial",
                )
                if key in device_info
            }
        )

    # Update cache
    with runtime.global_state.device_cache_lock:
        runtime.global_state.device_cache = {
            "devices": cache_records,
            "timestamp": cache_timestamp if cache_fresh else now,
        }

    return JSONResponse(content=devices_with_status)


router.include_router(bootloader_router)
router.include_router(operations_router)
router.include_router(ui_control_router)


# ==================== 设备分组：按属性自动分组 ====================


# 自动分组可用的属性维度 -> get_device_info 返回的 base_info 键
_AUTO_GROUP_KEYS = {
    "model": "model",
    "android_version": "android_version",
    "soc": "soc_model",
    "worker": "worker_id",
}


@router.post("/api/device-groups/auto")
@handle_api_errors
async def auto_group_devices(request: Request, req: dict = Body(default={})):
    """按设备属性（model / android_version / soc / worker）一键生成分组定义（per-user）。

    结果写回当前用户的 device_groups（变成可再手动微调的持久分组），并返回新分组列表。
    每个分组名形如 "model: Pixel 7"，id 形如 "auto_<dim>_<sanitized>"。
    """
    from features.users import (
        cluster_device_properties,
        current_username_for_request,
        enforce_exclusive_device_group,
        load_device_groups,
        normalize_device_groups,
        save_device_groups,
        soc_series,
    )

    dim = (req.get("by") or "").strip()
    info_key = _AUTO_GROUP_KEYS.get(dim)
    if dim != "worker" and not info_key:
        return _api_error(
            "by 必须是 model/android_version/soc/worker", status_code=400
        )

    # worker 维度：用集群设备池确定每台设备归属的主机，不需要 SSH 读属性
    if dim == "worker":
        value_to_devices: dict[str, list[str]] = {}
        local_worker_id = "ats-worker-controller"
        try:
            from features.cluster import get_cluster_service
            cluster = get_cluster_service()
            local_worker_id = cluster.config.local_worker_id
        except Exception:
            pass
        # 本地设备：用裸 serial（与 /api/devices/management 返回的 device_id 一致）
        local_devices = await asyncio.to_thread(device_manager.get_connected_devices)
        for device_id in local_devices:
            value_to_devices.setdefault(local_worker_id, []).append(device_id)
        # 远端 Worker 设备：用集群命名空间 ID（worker-id:serial）及友好主机名
        for device_id, properties in cluster_device_properties().items():
            source_host = properties.get("source_host") or "unknown"
            value_to_devices.setdefault(source_host, []).append(device_id)
        # 将 worker_id 映射为友好名称
        worker_names: dict[str, str] = {}
        try:
            for worker in get_cluster_service().list_workers():
                worker_names[worker["id"]] = worker.get("name") or worker["id"]
        except Exception:
            pass
        # Controller 使用统一 Worker ID，避免设备分组继续显示 user@host 旧名称。
        worker_names[local_worker_id] = local_worker_id
        # 重写本地 Worker 的 key；远端项已经使用友好主机名。
        _named = {}
        for wid, devs in value_to_devices.items():
            _named[worker_names.get(wid, wid)] = devs
        value_to_devices = _named
        if not value_to_devices:
            return _api_success({"groups": []}, "当前无在线设备")
    else:
        # 当前在线设备（用缓存即可，避免重复 SSH 扫描）
        raw_devices = await asyncio.to_thread(device_manager.get_connected_devices)
        # 收集每台设备的属性值
        value_to_devices = {}
        if raw_devices:
            with SSHConnection() as ssh:
                for device_id in raw_devices:
                    base_info = await asyncio.to_thread(
                        device_manager.get_device_info, device_id, ssh
                    )
                    value = str(base_info.get(info_key) or "未知").strip() or "未知"
                    if dim == "soc":
                        value = soc_series(value)
                    value_to_devices.setdefault(value, []).append(device_id)

        # 集群设备属性由 Worker 心跳上报，无需从 Controller SSH 到远端主机。
        for device_id, properties in cluster_device_properties().items():
            value = str(properties.get(info_key) or "未知").strip() or "未知"
            if dim == "soc":
                value = soc_series(value)
            value_to_devices.setdefault(value, []).append(device_id)
        if not value_to_devices:
            return _api_success({"groups": []}, "当前无在线设备")

    def _sanitize(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "unknown"

    username = current_username_for_request(request)
    existing_groups = load_device_groups(username)
    # 去掉所有旧的自动分组（不限维度），保留用户手建分组
    kept = [
        g for g in existing_groups
        if not str(g.get("id", "")).startswith("auto_")
    ]

    new_groups = list(kept)
    for value, devs in value_to_devices.items():
        gid = f"auto_{dim}_{_sanitize(value)}"
        # 若与手建分组 id 冲突则跳过该值，避免覆盖用户数据
        if any(g["id"] == gid for g in new_groups):
            gid = f"{gid}_{devs[0][:4]}"
        new_groups.append({
            "id": gid,
            "name": f"{dim}: {value}",
            "device_ids": devs,
        })

    # 规整一次（补 color、去重 device_ids、followed 默认 False）
    new_groups = normalize_device_groups(new_groups)
    # 互斥语义：自动分出的组之间互斥（同一设备只进一个 auto 组），也从手建组抢回设备
    for g in new_groups:
        if g["id"].startswith(f"auto_{dim}_"):
            enforce_exclusive_device_group(new_groups, g["id"], g["device_ids"])
    save_device_groups(username, new_groups)

    return _api_success({"groups": new_groups}, f"已按 {dim} 生成 {len(value_to_devices)} 个分组")
