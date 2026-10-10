"""USB/IP + ADB forward integration API (aggregation module).

路由实现按功能组拆分到 usbip_status_api / usbip_connect_api /
usbip_disconnect_api 等模块；本模块只负责：

1. 聚合 router（bootstrap/routes.py 挂载点不变）；
2. 保留跨模块消费者仍引用的符号：assistant 工具的
   ``features.devices.integrations_api:<symbol>`` 惰性映射、
   ``features.devices`` 惰性导出（annotate_cluster_usbip_devices 等）、
   usbip_status_reconcile.py 的 assignments helpers，以及测试 patch 的
   ``_usbip_assignments`` / ``_local_worker_id``。
"""
from __future__ import annotations

import asyncio as asyncio
import time as time

from fastapi import APIRouter

from . import reconnect as reconnect
from . import runtime as runtime
from .adb_forward_api import router as adb_forward_router
from .adb_forward_api import start_adb_forward as start_adb_forward
from .adb_forward_api import stop_adb_forward as stop_adb_forward
from .manager import device_manager as device_manager
from .usbip import usbip_manager as usbip_manager
from .usbip_assignments import (
    load_usbip_assignments as _usbip_assignments,
)
from .usbip_assignments import (
    save_usbip_assignments as _save_usbip_assignments,
)
from .usbip_assignments import (
    usbip_assignment_key as _usbip_assignment_key,
)
from .usbip_assignments import (
    usbip_assignment_lock as _usbip_assignment_lock,
)
from .usbip_connect_api import (
    _is_usbip_export_busy as _is_usbip_export_busy,
)
from .usbip_connect_api import (
    _is_usbip_recoverable_attach_error as _is_usbip_recoverable_attach_error,
)
from .usbip_connect_api import router as usbip_connect_router
from .usbip_connect_api import start_usbip as start_usbip
from .usbip_disconnect_api import router as usbip_disconnect_router
from .usbip_disconnect_api import stop_usbip as stop_usbip
from .usbip_install_api import install_usbipd
from .usbip_install_api import router as usbip_install_router
from .usbip_status_api import (
    _retire_assignments_for_physically_local_devices as _retire_assignments_for_physically_local_devices,
)
from .usbip_status_api import get_usbip_status as get_usbip_status
from .usbip_status_api import list_usbip_assignments as list_usbip_assignments
from .usbip_status_api import router as usbip_status_router
from .usbip_support import (
    _detach_ubuntu_usbip_for_devices as _detach_ubuntu_usbip_for_devices,
)
from .usbip_support import _local_worker_id as _local_worker_id
from .usbip_support import (
    _mark_usbip_source_disconnected as _mark_usbip_source_disconnected,
)
from .usbip_support import _usbip_devices_for_host as _usbip_devices_for_host
from .usbip_support import annotate_cluster_usbip_devices as annotate_cluster_usbip_devices
from .usbip_support import reconcile_cluster_usbip_command as reconcile_cluster_usbip_command
from .usbip_support import reconcile_cluster_usbip_heartbeat as reconcile_cluster_usbip_heartbeat


router = APIRouter()
router.include_router(adb_forward_router)
router.include_router(usbip_install_router)
router.include_router(usbip_status_router)
router.include_router(usbip_connect_router)
router.include_router(usbip_disconnect_router)

__all__ = [
    "_local_worker_id",
    "_save_usbip_assignments",
    "_usbip_assignment_key",
    "_usbip_assignment_lock",
    "_usbip_assignments",
    "annotate_cluster_usbip_devices",
    "get_usbip_status",
    "install_usbipd",
    "reconcile_cluster_usbip_command",
    "reconcile_cluster_usbip_heartbeat",
    "router",
    "start_adb_forward",
    "start_usbip",
    "stop_adb_forward",
    "stop_usbip",
]
