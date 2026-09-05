"""
Agent Response Generator — 将 ToolResult 转为前端可渲染的消息。

消息类型: text, table, status, plan, action_menu, code, error
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from features.assistant.executor import ToolResult


PAGE_DISPLAY_NAMES: dict[str, str] = {
    "test": "测试界面",
    "desktop": "主机桌面",
    "terminal": "主机终端",
    "users": "用户管理",
    "devices": "设备管理",
    "reports": "报告管理",
    "report-analysis": "报告分析",
    "apk-analysis": "APK分析",
    "test-suites": "测试套件",
    "api-docs": "系统接口",
    "architecture": "系统架构",
    "websites": "常用网址",
    "tools": "常用工具",
    "security-audit": "安全审计",
    "gms-assistant": "GMS助手",
    "automation": "GMS ATS",
    "cluster": "主机集群",
    "redmine-agent": "Redmine看板",
    "gerrit-dashboard": "Gerrit看板",
    "notes": "个人知识库",
    "agent": "对话Agent",
}


def page_quick_actions() -> list[dict[str, str]]:
    """Return one navigation quick action per first-class sidebar page."""
    return [{"label": label, "page": page} for page, label in PAGE_DISPLAY_NAMES.items()]


@dataclass
class AgentResponse:
    """Agent 回复消息。"""
    content: str                       # 纯文本回退
    kind: str                          # text / table / status / plan / action_menu / code / error
    data: dict[str, Any]               # 结构化数据
    quick_actions: list[dict[str, Any]]  # 快捷操作按钮
    page: str                          # 关联页面

    def to_message_data(self) -> dict[str, Any]:
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


def generate_clarification(suggestions: list[dict[str, str]]) -> AgentResponse:
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
        "我是 GMS 远程测试平台的对话 Agent，负责把自然语言转换成 Web_app 查询、导航和执行计划。\n\n"
        "我能做这些事：\n"
        "- 查询状态：设备/型号、空闲占用、测试状态、测试套件、报告、ATS 流水线、Cluster Worker/任务、构建任务、知识库、在线用户、系统健康、VPN、USB/IP、配置摘要。\n"
        "- 执行操作：启动/停止测试、失败 retry、取消/重试 ATS 或集群任务、连接 WiFi、重启/remount/投屏设备、VPN/USB-IP 操作、知识文档创建、报告下载/删除等；有风险或会改状态的操作会先让你确认。\n"
        "- 分析问题：报告分析、失败用例诊断、APK/JAR 反编译、套件 APK 源码分析、OpenGrok 代码搜索。\n"
        "- 页面导航：打开测试界面、设备管理、报告管理、报告分析、APK分析、测试套件、GMS ATS、主机集群、Redmine看板、Gerrit看板、个人知识库、终端、桌面、常用网址、常用工具、安全审计。\n\n"
        "你可以直接这样说：\n"
        "- 「rk3572设备」或「空闲设备」\n"
        "- 「最近报告」或「分析最新失败报告」\n"
        "- 「跑 CtsWifiTestCases，失败 retry 2 次」\n"
        "- 「打开 APK 分析」或「VPN 状态」"
    )


def generate_page_overview() -> str:
    """生成 Web_app 页面功能说明。"""
    return (
        "Web_app 主要页面功能如下：\n\n"
        "- 测试界面：选择 CTS/GTS/VTS/STS 等套件，指定模块/用例和设备，启动/停止测试，查看实时日志和执行状态。\n"
        "- 主机桌面：启动或查看 noVNC/x11vnc 桌面，用于远程 GUI 操作、调试工具和桌面环境确认。\n"
        "- 主机终端：打开服务器 SSH 终端，执行命令、上传文件、辅助定位环境问题。\n"
        "- 用户管理：查看在线用户、客户端 IP、用户名、测试运行状态和设备占用情况。\n"
        "- 设备管理：查看 ADB 设备、型号、Android 版本、电量、来源、锁定状态；支持重启、remount、WiFi、投屏、bootloader 操作。\n"
        "- 报告管理：列出历史测试报告，按用户/时间查看，下载、删除、进入分析。\n"
        "- 报告分析：上传报告或打开已有报告，解析失败项，做失败诊断、AI 分析和根因线索整理。\n"
        "- APK分析：上传 APK/JAR，反编译源码，查看 Manifest、权限、源码树和文件内容。\n"
        "- 测试套件：浏览本地/远端套件目录，查看 tradefed 结果，下载/解压套件，触发套件内 APK/JAR 分析。\n"
        "- 系统接口：查看 API 清单、参数、curl 示例和响应格式，适合调试接口或脚本调用。\n"
        "- 系统架构：查看平台模块、数据流和核心组件关系。\n"
        "- 常用网址：按分类维护常用站点、图标和链接。\n"
        "- 常用工具：维护可下载工具条目，从服务器白名单工具目录下载脚本或二进制工具。\n"
        "- 安全审计：查看页面访问、API 调用、请求摘要、响应摘要和耗时，辅助追踪操作记录。\n"
        "- GMS助手：打开外部/内置 GMS 知识助手入口。\n"
        "- GMS ATS：把 Gerrit 触发、构建产物、设备选择、烧写、测试执行和结果回写串成自动化测试站流程。\n"
        "- 主机集群：查看 Controller/Worker、设备、套件、运行任务和部署状态，管理持久化 Cluster Job。\n"
        "- Redmine看板：查看个人/部门/项目 Redmine 统计、待回复、超阈值未回复、解决趋势和问题明细。\n"
        "- Gerrit看板：查看个人/部门 Gerrit 提交统计、查询变更、趋势明细和成员配置。\n"
        "- 个人知识库：按知识空间和目录维护 Wiki，上传附件、全文检索、关联报告/Redmine/Gerrit 并进行知识问答。\n"
        "- 对话Agent：用自然语言查询设备/报告/套件，生成测试计划，打开项目页面，执行确认类操作并跟踪分析流程。"
    )


# ==================== Helpers ====================

def _page_display_name(page: str) -> str:
    """页面标识 → 中文显示名。"""
    return PAGE_DISPLAY_NAMES.get(page, page)
