"""Backward-compatible re-export shim for the former monolithic inventory module.

inventory.py 在 2026-08 审核第七节拆分后仅保留稳定导入面：
- 设备探测 / USB/IP / 设备操作 / 固件与 GSI 烧写 / 主机指标 → device_actions
- 套件执行 / 导出 / 报告导入 / 套件扫描 → suite_actions

既有调用方（app.py、features/cluster/*、tests）继续从本模块导入。
"""

from __future__ import annotations

from .device_actions import (  # noqa: F401 - re-export 保持既有导入路径稳定
    execute_device_action,
    execute_usbip_action,
    flash_firmware,
    flash_gsi,
    host_metrics,
    probe_devices,
)
from .suite_actions import (  # noqa: F401
    execute_suite_action,
    import_suite_report,
    prepare_suite_export,
    scan_suites,
)
