"""Agent 工具执行结果。

从 executor.py 拆出：ToolResult 同时被 executor 编排层、
route_invocation 执行层与 response 渲染层消费，独立成模块
避免执行层反向依赖编排层。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """工具执行结果。"""
    success: bool
    tool_name: str
    data: Any = None
    formatted_text: str = ""
    quick_actions: list[dict[str, Any]] = field(default_factory=list)
    page: str = ""
    kind: str = "text"  # text / table / status / file / code
    entities: dict[str, list[str]] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "data": self.data,
            "formatted_text": self.formatted_text,
            "quick_actions": self.quick_actions,
            "page": self.page,
            "kind": self.kind,
            "entities": self.entities,
            "error": self.error,
        }


def internal_error_result(tool_name: str, action_label: str) -> ToolResult:
    """未知异常的统一 Agent 出口。

    ``str(e)`` 可能内嵌路径/凭据片段，不外显给 Agent——完整 traceback
    只进服务端日志；Agent 拿到统一错误 + request id，用户可凭
    request_id 回查日志定位。``action_label`` 是失败动作的动词短语
    （执行/调用），用于 formatted_text。
    """
    request_id = uuid.uuid4().hex[:12]
    return ToolResult(
        success=False,
        tool_name=tool_name,
        error=f"服务内部错误（request_id={request_id}）",
        formatted_text=(
            f"{action_label}失败：服务内部错误（request_id={request_id}，已记录日志）"
        ),
    )


__all__ = ["ToolResult", "internal_error_result"]
