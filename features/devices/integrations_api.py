"""USB/IP + ADB forward integration API (aggregation module).

路由实现按功能组拆分到 usbip/status_api / usbip/connection_api 等模块；本模块只负责：

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
from .adb.api import router as adb_forward_router
from .adb.api import start_adb_forward as start_adb_forward
from .adb.api import stop_adb_forward as stop_adb_forward
from .manager import device_manager as device_manager
from .usbip.assignments import (
    load_usbip_assignments as _usbip_assignments,
)
from .usbip.assignments import (
    save_usbip_assignments as _save_usbip_assignments,
)
from .usbip.assignments import (
    usbip_assignment_key as _usbip_assignment_key,
)
from .usbip.assignments import (
    usbip_assignment_lock as _usbip_assignment_lock,
)
from .usbip.connection_api import (
    _is_usbip_export_busy as _is_usbip_export_busy,
)
from .usbip.connection_api import (
    _is_usbip_recoverable_attach_error as _is_usbip_recoverable_attach_error,
)
from .usbip.connection_api import router as usbip_connection_router
from .usbip.connection_api import start_usbip as start_usbip
from .usbip.connection_api import stop_usbip as stop_usbip
from .usbip.install_api import install_usbipd
from .usbip.install_api import router as usbip_install_router
from .usbip.manager import usbip_manager as usbip_manager
from .usbip.status_api import (
    _retire_assignments_for_physically_local_devices as _retire_assignments_for_physically_local_devices,
)
from .usbip.status_api import get_usbip_status as get_usbip_status
from .usbip.status_api import list_usbip_assignments as list_usbip_assignments
from .usbip.status_api import router as usbip_status_router
from .usbip.support import (
    _detach_ubuntu_usbip_for_devices as _detach_ubuntu_usbip_for_devices,
)
from .usbip.support import _local_worker_id as _local_worker_id
from .usbip.support import (
    _mark_usbip_source_disconnected as _mark_usbip_source_disconnected,
)
from .usbip.support import _usbip_devices_for_host as _usbip_devices_for_host
from .usbip.support import annotate_cluster_usbip_devices as annotate_cluster_usbip_devices
from .usbip.support import reconcile_cluster_usbip_command as reconcile_cluster_usbip_command
from .usbip.support import reconcile_cluster_usbip_heartbeat as reconcile_cluster_usbip_heartbeat


router = APIRouter()
router.include_router(adb_forward_router)
router.include_router(usbip_install_router)
router.include_router(usbip_status_router)
router.include_router(usbip_connection_router)

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
