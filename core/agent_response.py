"""
Agent Response Generator — 将 ToolResult 转为前端可渲染的消息。

消息类型: text, table, status, plan, action_menu, code, error
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.agent_executor import ToolResult


# ==================== Response ====================

from dataclasses import dataclass


@dataclass
class AgentResponse:
    """Agent 回复消息。"""
    content: str                       # 纯文本回退
    kind: str                          # text / table / status / plan / action_menu / code / error
    data: Dict[str, Any]               # 结构化数据
    quick_actions: List[Dict[str, Any]]  # 快捷操作按钮
    page: str                          # 关联页面

    def to_message_data(self) -> Dict[str, Any]:
        """转为 _append_message 的 data 参数。"""
        result = dict(self.data or {})
        if self.quick_actions:
            result["quick_actions"] = self.quick_actions
        if self.page:
            result["page"] = self.page
        return result


def generate(result: ToolResult) -> AgentResponse:
    """从 ToolResult 生成 AgentResponse。"""
    kind = result.kind or "text"
    quick_actions = [a for a in result.quick_actions if a] if result.quick_actions else []

    # 根据 kind 增强 quick_actions
    if not quick_actions and result.page:
        quick_actions = [{"label": f"打开{_page_display_name(result.page)}", "page": result.page}]

    return AgentResponse(
        content=result.formatted_text or (result.error if not result.success else "操作完成"),
        kind=kind,
        data=result.data or {},
        quick_actions=quick_actions,
        page=result.page,
    )


def generate_error(error: str, suggestions: Optional[List[str]] = None) -> AgentResponse:
    """生成错误响应。"""
    content = f"❌ {error}"
    if suggestions:
        content += "\n\n您可以尝试：\n" + "\n".join(f"- {s}" for s in suggestions)
    return AgentResponse(
        content=content,
        kind="error",
        data={"error": error, "suggestions": suggestions or []},
        quick_actions=[],
        page="",
    )


def generate_clarification(suggestions: List[Dict[str, str]]) -> AgentResponse:
    """生成澄清响应（当意图置信度低时）。"""
    lines = ["我不太确定您想要做什么。您是想要："]
    for i, s in enumerate(suggestions[:4], 1):
        lines.append(f"{i}. {s.get('label', s.get('description', ''))}")
    return AgentResponse(
        content="\n".join(lines),
        kind="action_menu",
        data={"suggestions": suggestions},
        quick_actions=[
            {"label": s.get("label", ""), "action": s.get("action", ""), "params": s.get("params", {})}
            for s in suggestions[:4]
        ],
        page="",
    )


def generate_capability_overview() -> str:
    """生成 Agent 能力概览文本。"""
    return (
        "我现在按 Web_app 功能分两类处理：\n"
        "1. **查询类**直接回答：设备、测试套件、报告、APK任务、用户、测试状态、系统健康、配置摘要。\n"
        "2. **执行类**先生成计划，确认后执行：启动测试、失败 retry、报告诊断、APK/源码分析。\n\n"
        "直接用中文告诉我要做什么！例如：\n"
        "- 「有几台设备」→ 返回设备列表\n"
        "- 「测试套件」→ 返回套件列表\n"
        "- 「跑 CtsWifiTestCases」→ 生成测试计划\n"
        "- 「VPN 状态」→ 返回 VPN 连接状态\n"
        "- 「打开终端」→ 跳转到终端页面"
    )


# ==================== Helpers ====================

def _page_display_name(page: str) -> str:
    """页面标识 → 中文显示名。"""
    names = {
        "test": "测试界面", "desktop": "主机桌面", "terminal": "主机终端",
        "users": "用户管理", "devices": "设备管理", "reports": "报告管理",
        "report-analysis": "报告分析", "apk-analysis": "APK分析",
        "test-suites": "测试套件", "api-docs": "系统接口",
        "architecture": "系统架构", "tools": "常用网址",
        "security-audit": "安全审计", "gms-assistant": "GMS助手",
        "agent": "对话Agent",
    }
    return names.get(page, page)
