"""Knowledge 类 Agent 工具（从 features/assistant/tools.py 拆出）。

归属 assistant 侧（工具注册是 assistant 的职责）；ADR 0014 的
``android_internals_search`` 只读工具在这里登记，描述明确标注它是
Android 系统机制**背景知识**（非内部案例、非根因证据）。
"""

from __future__ import annotations

from typing import Any

from features.assistant.tools import _CATEGORY_KEYWORDS, AgentTool


def _kw(category: str, *extra: str) -> list[str]:
    kws = list(_CATEGORY_KEYWORDS.get(category, []))
    kws.extend(extra)
    return list(dict.fromkeys(kws))


def knowledge_agent_tools() -> list[AgentTool]:
    """knowledge 类工具定义（注册顺序即返回顺序）。"""
    return [
        AgentTool(
            name="knowledge_search",
            category="knowledge",
            description="搜索个人知识库 Wiki 文档",
            api_path="/api/knowledge/search",
            method="GET",
            params=[
                {"name": "q", "type": "string", "required": True, "desc": "搜索关键词"},
                {"name": "space_id", "type": "string", "required": False, "desc": "知识空间 ID"},
                {"name": "limit", "type": "integer", "required": False, "desc": "返回数量"},
            ],
            keywords=_kw("knowledge", "搜索知识库", "Wiki搜索", "查文档"),
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="features.knowledge.api:search_docs",
            response_type="list",
        ),
        AgentTool(
            name="knowledge_ask",
            category="knowledge",
            description="基于个人知识库进行问答",
            api_path="/api/knowledge/ask",
            method="POST",
            params=[
                {"name": "question", "type": "string", "required": True, "desc": "问题"},
                {"name": "space_id", "type": "string", "required": False, "desc": "知识空间 ID"},
            ],
            keywords=_kw("knowledge", "知识库问答", "问Wiki", "基于文档回答"),
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="features.knowledge.api:ask_knowledge",
            response_type="detail",
        ),
        AgentTool(
            name="knowledge_create",
            category="knowledge",
            description="创建个人知识库 Wiki 文档",
            api_path="/api/knowledge/docs",
            method="POST",
            params=[
                {"name": "title", "type": "string", "required": False, "desc": "文档标题"},
                {"name": "content", "type": "string", "required": True, "desc": "文档内容"},
                {"name": "space_id", "type": "string", "required": False, "desc": "知识空间 ID"},
                {"name": "tags", "type": "array", "required": False, "desc": "标签"},
            ],
            keywords=_kw("knowledge", "创建知识文档", "写Wiki", "新增文档"),
            is_readonly=False,
            is_dangerous=False,
            requires_confirm=True,
            executor_ref="features.knowledge.api:create_doc",
            response_type="detail",
        ),
        AgentTool(
            name="android_internals_search",
            category="knowledge",
            description=(
                "搜索 Android 系统机制背景知识（android-internals-wiki，非内部案例）。"
                "命中后按 📚机制解释（引用 chapter/适用版本/last_verified）→ "
                "🧪结合当前测试现象 → 🔍建议补充的设备/源码取证 组织回答；"
                "背景知识不得作为已证实根因"
            ),
            api_path="/api/knowledge/external/search",
            method="POST",
            params=[
                {"name": "query", "type": "string", "required": True, "desc": "机制/关键词，如 LMKD、Binder 超时"},
                {"name": "sources", "type": "array", "required": False, "desc": "限定外部知识源（默认全部）"},
                {"name": "limit", "type": "integer", "required": False, "desc": "返回数量（1-10）"},
                {"name": "android_api_level", "type": "integer", "required": False,
                 "desc": "目标 Android API level（A13/14/15/16/17 → 33/34/35/36/37），用于版本匹配重排"},
            ],
            keywords=_kw(
                "knowledge", "系统机制", "原理", "Android机制", "LMKD", "Binder",
                "ANR", "内存", "SurfaceFlinger", "Perfetto", "背景知识",
            ),
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="features.knowledge.external_api:search_external",
            response_type="list",
        ),
    ]


def register_knowledge_agent_tools(registry: Any) -> None:
    """注册 knowledge 类 Agent 工具；同名工具不重复注册。"""
    for tool in knowledge_agent_tools():
        if not registry.get(tool.name):
            registry.register(tool)
