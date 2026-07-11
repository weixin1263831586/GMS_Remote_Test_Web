"""Android CLI tools exposed to the assistant without growing the legacy registry."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from features.assistant.tools import ToolRegistry


def register_android_ui_tools(registry: ToolRegistry) -> None:
    from features.assistant.tools import AgentTool

    definitions = (
        (
            "devices_ui_layout",
            "读取指定Android设备当前页面的结构化UI元素和坐标",
            "/api/devices/ui/layout",
            [{"name": "serial", "type": "string", "required": True, "desc": "设备序列号"}],
            ["UI布局", "页面元素", "控件", "android layout", "界面识别"],
            True,
            False,
            "features.devices.ui_control_api:ui_layout",
        ),
        (
            "devices_ui_tap",
            "在指定Android设备上点按UI坐标",
            "/api/devices/ui/tap",
            [
                {"name": "serial", "type": "string", "required": True, "desc": "设备序列号"},
                {"name": "x", "type": "integer", "required": True, "desc": "横坐标"},
                {"name": "y", "type": "integer", "required": True, "desc": "纵坐标"},
            ],
            ["点击控件", "点按", "tap", "点击坐标", "UI操作"],
            False,
            True,
            "features.devices.ui_control_api:ui_tap",
        ),
    )
    for name, description, path, params, keywords, readonly, confirm, executor_ref in definitions:
        registry.register(AgentTool(
            name=name,
            category="device",
            description=description,
            api_path=path,
            method="POST",
            params=params,
            keywords=keywords,
            is_readonly=readonly,
            is_dangerous=False,
            requires_confirm=confirm,
            executor_ref=executor_ref,
            response_type="detail",
        ))
