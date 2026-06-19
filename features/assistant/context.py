"""
Agent Context Manager — 多轮对话上下文管理。

上下文存储在 session dict 的 "context" key 中，向后兼容旧会话。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ==================== Context Structure ====================

def new_context() -> Dict[str, Any]:
    """创建新的空上下文。"""
    return {
        "last_tool": "",
        "last_category": "",
        "last_entities": {},       # {"devices": [...], "reports": [...], ...}
        "last_result_count": 0,
        "last_result_summary": "",
        "turns": [],               # [{"role": "user"|"assistant", "summary": "...", "tool": "..."}]
    }


def get_context(session: Dict[str, Any]) -> Dict[str, Any]:
    """获取会话上下文，如果不存在则创建。"""
    if "context" not in session:
        session["context"] = new_context()
    return session["context"]


# ==================== Update Context ====================

def update_context(
    session: Dict[str, Any],
    *,
    tool_name: str = "",
    category: str = "",
    entities: Optional[Dict[str, List[str]]] = None,
    result_count: int = 0,
    result_summary: str = "",
    user_summary: str = "",
) -> None:
    """在工具执行后更新上下文。"""
    ctx = get_context(session)

    if tool_name:
        ctx["last_tool"] = tool_name
    if category:
        ctx["last_category"] = category
    if entities:
        # 合并而不是替换，保留其他类型的实体
        existing = ctx.get("last_entities", {})
        for key, values in entities.items():
            existing[key] = values
        ctx["last_entities"] = existing
    if result_count is not None:
        ctx["last_result_count"] = result_count
    if result_summary:
        ctx["last_result_summary"] = result_summary

    # 记录轮次
    turns = ctx.get("turns", [])
    if user_summary:
        turns.append({"role": "user", "summary": user_summary[:200]})
    if tool_name:
        turns.append({
            "role": "assistant",
            "tool": tool_name,
            "summary": result_summary[:200] if result_summary else tool_name,
        })
    # 保留最近 20 轮
    ctx["turns"] = turns[-20:]


def record_user_message(session: Dict[str, Any], message: str) -> None:
    """记录用户消息到上下文轮次。"""
    ctx = get_context(session)
    turns = ctx.get("turns", [])
    turns.append({"role": "user", "summary": message[:200]})
    ctx["turns"] = turns[-20:]


# ==================== Reference Resolution ====================

def resolve_reference(session: Dict[str, Any], text: str) -> Dict[str, Any]:
    """解析代词和引用（"它"、"第一个"、"那个报告"等）为具体实体。

    返回 {"resolved": True/False, "entities": {...}, "intent_hint": "..."}
    """
    ctx = get_context(session)
    result: Dict[str, Any] = {"resolved": False, "entities": {}, "intent_hint": ""}

    entities = ctx.get("last_entities", {})
    last_tool = ctx.get("last_tool", "")
    last_category = ctx.get("last_category", "")

    # --- 代词解析 ---
    if re.search(r"它|它\d|这个|那个|这件|那件", text):
        result["resolved"] = True
        # 如果消息中包含类型提示词（"那个报告"、"这个设备"），优先匹配该类型
        if re.search(r"报告|report", text) and "reports" in entities:
            result["entities"] = {"reports": entities.get("reports", [])}
            result["intent_hint"] = "report_detail"
        elif re.search(r"设备|device", text) and "devices" in entities:
            result["entities"] = {"devices": entities.get("devices", [])}
            result["intent_hint"] = "device_detail"
        elif re.search(r"套件|suite", text) and "suites" in entities:
            result["entities"] = {"suites": entities.get("suites", [])}
            result["intent_hint"] = "suite_detail"
        elif re.search(r"任务|task", text) and "tasks" in entities:
            result["entities"] = {"tasks": entities.get("tasks", [])}
            result["intent_hint"] = "apk_detail"
        # 否则按上次工具类型决定
        elif last_category in ("device",) or "devices" in entities:
            result["entities"] = {"devices": entities.get("devices", [])}
            result["intent_hint"] = "device_detail"
        elif last_category in ("report",) or "reports" in entities:
            result["entities"] = {"reports": entities.get("reports", [])}
            result["intent_hint"] = "report_detail"
        elif last_category in ("test",) or "suites" in entities:
            result["entities"] = {"suites": entities.get("suites", [])}
            result["intent_hint"] = "suite_detail"
        elif last_category in ("apk",) or "tasks" in entities:
            result["entities"] = {"tasks": entities.get("tasks", [])}
            result["intent_hint"] = "apk_detail"
        else:
            # 通用：返回所有实体
            result["entities"] = entities

    # --- 序数词解析 ---
    ordinal_match = re.search(r"第([一二三四五六七八九十\d]+)[个台份项]", text)
    if ordinal_match:
        idx = _parse_chinese_number(ordinal_match.group(1)) - 1  # 0-indexed
        result["resolved"] = True

        # 优先使用最近类别的实体列表
        for entity_key in ("devices", "reports", "suites", "tasks", "users"):
            items = entities.get(entity_key, [])
            if items and 0 <= idx < len(items):
                result["entities"] = {entity_key: [items[idx]]}
                result["intent_hint"] = f"{entity_key}_detail"
                break

    # --- "详细信息/详情" 解析 ---
    if re.search(r"详细|详情|更多信息|详细信|具体", text) and not result["resolved"]:
        result["resolved"] = True
        result["entities"] = entities
        # 根据上次分类推断
        if last_category == "device":
            result["intent_hint"] = "device_detail"
        elif last_category == "report":
            result["intent_hint"] = "report_detail"
        else:
            result["intent_hint"] = f"{last_category}_detail" if last_category else ""

    # --- "下载" 解析（针对上次的报告/文件） ---
    if re.search(r"下载|download", text) and not result["resolved"]:
        if "reports" in entities and entities["reports"]:
            result["resolved"] = True
            result["entities"] = {"reports": entities["reports"][:1]}
            result["intent_hint"] = "report_download"

    # --- "删除" 解析 ---
    if re.search(r"删除|删掉|移除|delete|remove", text) and not result["resolved"]:
        if "reports" in entities and entities["reports"]:
            result["resolved"] = True
            result["entities"] = {"reports": entities["reports"][:1]}
            result["intent_hint"] = "report_delete"

    return result


# ==================== Context Queries ====================

def get_last_entities(session: Dict[str, Any], entity_type: str) -> List[str]:
    """获取上次记录的某类实体。"""
    ctx = get_context(session)
    return ctx.get("last_entities", {}).get(entity_type, [])


def get_conversation_summary(session: Dict[str, Any], last_n: int = 4) -> str:
    """获取最近 N 轮对话的文本摘要。"""
    ctx = get_context(session)
    turns = ctx.get("turns", [])[-last_n:]
    lines = []
    for turn in turns:
        role = "用户" if turn.get("role") == "user" else "Agent"
        lines.append(f"{role}: {turn.get('summary', '')}")
    return "\n".join(lines)


def get_last_tool(session: Dict[str, Any]) -> str:
    """获取上次执行的工具名。"""
    ctx = get_context(session)
    return ctx.get("last_tool", "")


# ==================== Helpers ====================

_CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_chinese_number(text: str) -> int:
    """解析中文数字。"""
    if text.isdigit():
        return int(text)
    for cn, num in _CN_NUMBERS.items():
        if cn in text:
            return num
    return 1
