"""USB/IP + ADB forward integration API (aggregation module).

2026-09 大文件收敛：路由实现按功能组拆分到
usbip_status_api / usbip_connect_api / usbip_disconnect_api，
共享 helper 在 usbip_support。本模块只负责：

1. 聚合 router（bootstrap/routes.py 挂载点不变）；
2. 再导出既有公开符号，保持
   ``features.devices.annotate_cluster_usbip_devices`` 等惰性映射与
   历史测试（``from .integrations_api import ...`` /
   ``patch("features.devices.integrations_api.X")``）继续可用。
"""
from __future__ import annotations

import asyncio as asyncio
import shlex as shlex
import time as time
import uuid as uuid

from fastapi import APIRouter

from . import reconnect as reconnect
from . import runtime as runtime
from .adb_forward_api import router as adb_forward_router
from .adb_forward_api import start_adb_forward as start_adb_forward
from .adb_forward_api import stop_adb_forward as stop_adb_forward
from .locks import device_lock_manager as device_lock_manager
from .manager import device_manager as device_manager
from .models import USBIPDisconnectRequest as USBIPDisconnectRequest
from .models import USBIPStartRequest as USBIPStartRequest
from .support import DeviceSSHConnection as DeviceSSHConnection
from .support import (
    acquire_device_operation_claim as acquire_device_operation_claim,
)
from .support import audit_device_operation as audit_device_operation
from .support import (
    format_device_list_info as format_device_list_info,
)
from .support import notify_device_change as notify_device_change
from .support import (
    release_device_operation_claim as release_device_operation_claim,
)
from .usbip import detach_ubuntu_usbip_ports as detach_ubuntu_usbip_ports
from .usbip import find_device_host_password as find_device_host_password
from .usbip import usbip_manager as usbip_manager
from .usbip_access import (
    enforce_usbip_host_access as enforce_usbip_host_access,
)
from .usbip_access import usbip_request_user as usbip_request_user
from .usbip_assignments import (
    load_usbip_assignments as _usbip_assignments,
)
from .usbip_assignments import (
    mark_usbip_detach_unknown as _mark_usbip_detach_unknown,
)
from .usbip_assignments import (
    next_transport_generation as _next_transport_generation,
)
from .usbip_assignments import (
    prune_stale_unknown_usbip_assignments as _prune_stale_unknown_usbip_assignments,
)
from .usbip_assignments import (
    reconcile_usbip_assignment_serials as _reconcile_usbip_assignment_serials,
)
from .usbip_assignments import (
    save_usbip_assignments as _save_usbip_assignments,
)
from .usbip_assignments import (
    usbip_assignment_key as _usbip_assignment_key,
)
from .usbip_connect_api import (
    _attached_usbip_serials as _attached_usbip_serials,
)
from .usbip_connect_api import (
    _is_usbip_export_busy as _is_usbip_export_busy,
)
from .usbip_connect_api import (
    _is_usbip_recoverable_attach_error as _is_usbip_recoverable_attach_error,
)
from .usbip_connect_api import (
    _preserve_usbip_assignment_after_error as _preserve_usbip_assignment_after_error,
)
from .usbip_connect_api import (
    _rollback_local_usbip_attach as _rollback_local_usbip_attach,
)
from .usbip_connect_api import (
    _usbip_worker_command_timeout as _usbip_worker_command_timeout,
)
from .usbip_connect_api import router as usbip_connect_router
from .usbip_connect_api import start_usbip as start_usbip
from .usbip_disconnect_api import router as usbip_disconnect_router
from .usbip_disconnect_api import stop_usbip as stop_usbip
from .usbip_install_api import install_usbipd
from .usbip_install_api import router as usbip_install_router
from .usbip_linux_source import (
    stop_ubuntu_usbip_server as stop_ubuntu_usbip_server,
)
from .usbip_operations import (
    has_remaining_usbip_assignments as has_remaining_usbip_assignments,
)
from .usbip_operations import (
    selected_usbip_serials as selected_usbip_serials,
)
from .usbip_operations import (
    serialize_usbip_operation as serialize_usbip_operation,
)
from .usbip_operations import usbip_error_fields as _usbip_error_fields
from .usbip_persistence import (
    lookup_usbip_source_os as _lookup_usbip_source_os,
)
from .usbip_persistence import (
    persist_local_usbip_sources as _persist_local_usbip_sources,
)
from .usbip_persistence import (
    record_usbip_network_quality as _record_usbip_network_quality,
)
from .usbip_persistence import (
    record_usbip_source_os as _record_usbip_source_os,
)
from .usbip_persistence import (
    usbip_assignment_lock as _usbip_assignment_lock,
)
from .usbip_status_api import (
    _reconcile_local_usbip_status as _reconcile_local_usbip_status,
)
from .usbip_status_api import (
    _retire_assignments_for_physically_local_devices as _retire_assignments_for_physically_local_devices,
)
from .usbip_status_api import (
    _verify_local_usbip_transport as _verify_local_usbip_transport,
)
from .usbip_status_api import get_usbip_source_os as get_usbip_source_os
from .usbip_status_api import get_usbip_status as get_usbip_status
from .usbip_status_api import list_usbip_assignments as list_usbip_assignments
from .usbip_status_api import list_usbip_source_devices as list_usbip_source_devices
from .usbip_status_api import router as usbip_status_router
from .usbip_support import (
    _adb_devices_on_ssh as _adb_devices_on_ssh,
)
from .usbip_support import (
    _adb_proxy_target_assignments as _adb_proxy_target_assignments,
)
from .usbip_support import (
    _clear_usbip_device_sources as _clear_usbip_device_sources,
)
from .usbip_support import (
    _detach_ubuntu_usbip_for_devices as _detach_ubuntu_usbip_for_devices,
)
from .usbip_support import (
    _invalidate_device_cache as _invalidate_device_cache,
)
from .usbip_support import (
    _local_worker_id as _local_worker_id,
)
from .usbip_support import (
    _mark_usbip_source_disconnected as _mark_usbip_source_disconnected,
)
from .usbip_support import (
    _persist_device_source_removal as _persist_device_source_removal,
)
from .usbip_support import (
    _resolve_usbip_device_host as _resolve_usbip_device_host,
)
from .usbip_support import (
    _usbip_devices_for_host as _usbip_devices_for_host,
)
from .usbip_support import (
    _usbip_remote_host as _usbip_remote_host,
)
from .usbip_support import (
    _wait_for_adb_devices_removed as _wait_for_adb_devices_removed,
)
from .usbip_support import annotate_cluster_usbip_devices as annotate_cluster_usbip_devices
from .usbip_support import reconcile_cluster_usbip_command as reconcile_cluster_usbip_command
from .usbip_support import reconcile_cluster_usbip_heartbeat as reconcile_cluster_usbip_heartbeat
from .usbip_transport_probe import (
    probe_existing_local_usbip_transport as probe_existing_local_usbip_transport,
)
from .utils import DeviceUtils as DeviceUtils


router = APIRouter()
router.include_router(adb_forward_router)
router.include_router(usbip_install_router)
router.include_router(usbip_status_router)
router.include_router(usbip_connect_router)
router.include_router(usbip_disconnect_router)

__all__ = [
    "DeviceSSHConnection",
    "DeviceUtils",
    "USBIPDisconnectRequest",
    "USBIPStartRequest",
    "_adb_devices_on_ssh",
    "_adb_proxy_target_assignments",
    "_attached_usbip_serials",
    "_clear_usbip_device_sources",
    "_invalidate_device_cache",
    "_local_worker_id",
    "_lookup_usbip_source_os",
    "_mark_usbip_detach_unknown",
    "_mark_usbip_source_disconnected",
    "_next_transport_generation",
    "_persist_device_source_removal",
    "_persist_local_usbip_sources",
    "_prune_stale_unknown_usbip_assignments",
    "_reconcile_local_usbip_status",
    "_reconcile_usbip_assignment_serials",
    "_record_usbip_network_quality",
    "_record_usbip_source_os",
    "_resolve_usbip_device_host",
    "_retire_assignments_for_physically_local_devices",
    "_rollback_local_usbip_attach",
    "_save_usbip_assignments",
    "_usbip_assignment_key",
    "_usbip_assignment_lock",
    "_usbip_assignments",
    "_usbip_devices_for_host",
    "_usbip_error_fields",
    "_usbip_remote_host",
    "_verify_local_usbip_transport",
    "_wait_for_adb_devices_removed",
    "acquire_device_operation_claim",
    "annotate_cluster_usbip_devices",
    "asyncio",
    "audit_device_operation",
    "detach_ubuntu_usbip_ports",
    "device_lock_manager",
    "device_manager",
    "enforce_usbip_host_access",
    "find_device_host_password",
    "format_device_list_info",
    "get_usbip_source_os",
    "get_usbip_status",
    "has_remaining_usbip_assignments",
    "install_usbipd",
    "list_usbip_assignments",
    "list_usbip_source_devices",
    "notify_device_change",
    "probe_existing_local_usbip_transport",
    "reconcile_cluster_usbip_command",
    "reconcile_cluster_usbip_heartbeat",
    "reconnect",
    "release_device_operation_claim",
    "router",
    "runtime",
    "selected_usbip_serials",
    "serialize_usbip_operation",
    "shlex",
    "start_adb_forward",
    "start_usbip",
    "stop_adb_forward",
    "stop_ubuntu_usbip_server",
    "stop_usbip",
    "time",
    "usbip_manager",
    "usbip_request_user",
    "uuid",
]
