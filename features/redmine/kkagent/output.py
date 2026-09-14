"""kkagent 输出解析：从最终信封/原始输出中提取晨报 JSON 并校验。

只做严格 json.loads，不做 regex 猜测修复；非法输出一律交由上层按
invalid_ai_output / schema_mismatch 处理（可触发精确 resume 修复）。
"""

from __future__ import annotations

import json
from typing import Any

from ..daily_brief_models import validate_issue_result
from .trace import KkAgentTrace


def extract_json(raw: str) -> dict[str, Any] | None:
    """从输出中提取 JSON 对象；只接受首个平衡的 {...}，不做猜测修复。"""
    text = raw.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    # stream-json / 混合输出：找最后一个完整 JSON 对象行。
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def json_from_message(message: Any) -> dict[str, Any] | None:
    """从 message 字符串提取 JSON 对象。

    接受三种形态（kkagent 0.4.x 实测均出现过）：
    - 裸 JSON 对象；
    - 整体包在 ```json ...``` 围栏内；
    - 说明散文 + 围栏 JSON（模型在证据不足时的常见输出）。

    只做严格 json.loads；提取不到返回 None，由上层报 invalid_ai_output。
    """
    if not isinstance(message, str):
        return None
    text = message.strip()
    fence_start = text.find("```")
    if fence_start >= 0:
        after = text[fence_start + 3:]
        if after.startswith("json"):
            after = after[4:]
        fence_end = after.find("```")
        if fence_end > 0:
            text = after[:fence_end].strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def envelope_result(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """kkagent 信封 → 模型最终 JSON 对象（没有则 None）。"""
    result = parsed.get("result") if isinstance(parsed.get("result"), dict) else None
    if result is None:
        # 信封把模型最终回复放在 message 字符串字段。可能是裸 JSON、
        # markdown 围栏，或围栏前带说明散文——统一交给 json_from_message。
        inner = json_from_message(parsed.get("message"))
        if isinstance(inner, dict):
            result = inner
    return result if isinstance(result, dict) else None


def parse_issue_result(
    *,
    trace: KkAgentTrace | None = None,
    raw: str = "",
) -> tuple[dict[str, Any] | None, list[str]]:
    """从轨迹的最终信封（优先）或原始输出（兜底）解析并校验晨报 JSON。

    返回 (result, errors)；result 为 None 时 errors 至少含一条
    invalid_ai_output / schema_mismatch 前缀的错误。
    """
    parsed: dict[str, Any] | None = None
    if trace is not None and isinstance(trace.final_event, dict):
        parsed = trace.final_event
    if parsed is None and raw:
        parsed = extract_json(raw)
    if parsed is None:
        return None, ["kkagent output is not valid JSON"]
    result = envelope_result(parsed) or parsed
    if not isinstance(result, dict):
        return None, ["kkagent output has no result object"]
    errors = validate_issue_result(result)
    if errors:
        return None, ["schema validation failed: " + "; ".join(errors)]
    return result, []


__all__ = ["envelope_result", "extract_json", "json_from_message", "parse_issue_result"]
