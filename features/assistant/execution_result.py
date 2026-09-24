"""Agent 工具执行结果。

从 executor.py 拆出：ToolResult 同时被 executor 编排层、
route_invocation 执行层与 response 渲染层消费，独立成模块
避免执行层反向依赖编排层。
"""

from __future__ import annotations

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


__all__ = ["ToolResult"]
