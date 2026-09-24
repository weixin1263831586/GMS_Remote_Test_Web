"""Read back one analysis session's kkagent transcript views.

「查看分析」弹框的「会话回放」数据源：把 Daily Brief 单条分析对应的
kkagent 会话（``~/.kkagent/transcripts.db`` 的 messages 表）按序展开成
回合流，供 UI 回放取证过程；用户显式切换后也可分页读取未裁剪原文。

安全边界（硬性）：
- session_id 一律来自该 owner 该 issue 的 ``redmine_daily_brief_ai_executions``
  记录（``ai_execution.session_id``），API 不接受调用方传入的任意 session id；
  不存在归属关系的会话一律 404，不泄露存在性。
- 默认回合视图裁剪后再下发，防止单条日志把响应与浏览器内存撑爆；
  ``raw`` 视图按消息分页且不裁剪文本/工具载荷。
- 两种视图都跳过模型内部 thinking/redacted_thinking，不把隐私推理当作
  可公开的会话证据。
- 会话内容是分析证据，只读展示；本模块不做任何写操作。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


# 单条工具输出的预览上限：足够看清关键报错，又不会把弹框渲染成日志页。
TOOL_OUTPUT_PREVIEW_CHARS = 2000
TOOL_INPUT_PREVIEW_CHARS = 600
TEXT_PREVIEW_CHARS = 4000
# 单页回合数：浏览器渲染上限优先，分页按钮负责翻页。
DEFAULT_PAGE_SIZE = 80
MAX_PAGE_SIZE = 200

_SKIPPED_BLOCK_TYPES = {"thinking", "redacted_thinking"}
# 会话回放关心的事件块：模型动作与结果。system/usage 等噪声不上屏。
_RENDERABLE_BLOCK_TYPES = {"text", "tool_use", "tool_result"}


def transcripts_db_path() -> Path:
    """kkagent 会话库路径；部署可用环境变量覆盖（systemd HOME 不同）。"""
    override = str(os.environ.get("KKAGENT_TRANSCRIPTS_DB") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".kkagent" / "transcripts.db"


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False
    )
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（截断，共 {len(text)} 字符）"


def _parse_message_blocks(blocks: Any) -> list[dict[str, Any]]:
    """解析一条 message，同时保留可见 block 的原始顺序。"""
    parsed: list[dict[str, Any]] = []
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            blocks = [{"type": "text", "text": str(blocks)}]
    if not isinstance(blocks, list):
        return parsed
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in _SKIPPED_BLOCK_TYPES:
            continue
        if block_type not in _RENDERABLE_BLOCK_TYPES:
            continue
        item: dict[str, Any] = {"kind": block_type}
        if block_type == "text":
            item["text"] = _clip(block.get("text"), TEXT_PREVIEW_CHARS)
        elif block_type == "tool_use":
            item["tool_call_id"] = str(block.get("id") or "")
            item["tool_name"] = str(block.get("name") or "")
            item["tool_input"] = _clip(
                block.get("input"), TOOL_INPUT_PREVIEW_CHARS
            )
        elif block_type == "tool_result":
            item["tool_call_id"] = str(block.get("tool_use_id") or "")
            item["is_error"] = bool(block.get("is_error"))
            item["output"] = _clip(
                block.get("content"), TOOL_OUTPUT_PREVIEW_CHARS
            )
        parsed.append(item)
    return parsed


def _untrimmed_message_content(value: Any) -> Any:
    """保留消息原文，只移除不可公开的内部推理 block。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return [
            item
            for item in value
            if not (
                isinstance(item, dict)
                and str(item.get("type") or "") in _SKIPPED_BLOCK_TYPES
            )
        ]
    if (
        isinstance(value, dict)
        and str(value.get("type") or "") in _SKIPPED_BLOCK_TYPES
    ):
        return []
    return value


def _attach_tool_results(
    turn: dict[str, Any] | None,
    results: list[dict[str, Any]],
    created_at: str,
) -> list[dict[str, Any]]:
    """把 user message 中的 tool_result 回挂到前一模型回合。"""
    if turn is None:
        return results
    tools = {
        str(block.get("tool_call_id") or ""): block
        for block in turn["blocks"]
        if block.get("kind") == "tool_use" and block.get("tool_call_id")
    }
    unmatched: list[dict[str, Any]] = []
    for result in results:
        tool = tools.get(str(result.get("tool_call_id") or ""))
        if tool is None:
            unmatched.append(result)
            continue
        tool["result"] = {
            "is_error": bool(result.get("is_error")),
            "output": str(result.get("output") or ""),
            "created_at": created_at,
        }
    return unmatched


def _iter_session_turns(
    rows: Iterable[tuple[Any, Any, Any]],
) -> Iterator[dict[str, Any]]:
    """把消息流聚合成接近 kkagent 终端语义的回合流。"""
    sequence = 0
    pending_assistant: dict[str, Any] | None = None

    for role_value, content_json, created_at_value in rows:
        role = str(role_value or "")
        created_at = str(created_at_value or "")
        blocks = _parse_message_blocks(content_json)
        results = [item for item in blocks if item["kind"] == "tool_result"]
        visible = [item for item in blocks if item["kind"] != "tool_result"]
        unmatched = _attach_tool_results(
            pending_assistant, results, created_at
        )
        if pending_assistant is not None and unmatched:
            # 极少数异常会话可能缺失对应 tool_use；仍保留真实结果，避免
            # 审计记录静默丢失，并在前端标成“未匹配工具结果”。
            pending_assistant["blocks"].extend(unmatched)
        elif unmatched:
            yield {
                "sequence": sequence,
                "kind": "tool_batch",
                "role": role,
                "created_at": created_at,
                "blocks": unmatched,
            }
            sequence += 1
            unmatched = []

        if not visible:
            continue

        if pending_assistant is not None:
            pending_assistant["sequence"] = sequence
            yield pending_assistant
            sequence += 1
            pending_assistant = None

        turn = {
            "kind": "assistant" if role == "assistant" else (
                "context" if sequence == 0 else "request"
            ),
            "role": role,
            "created_at": created_at,
            "blocks": visible,
        }
        if role == "assistant":
            pending_assistant = turn
            continue
        turn["sequence"] = sequence
        yield turn
        sequence += 1

    if pending_assistant is not None:
        pending_assistant["sequence"] = sequence
        pending_assistant["is_final"] = not any(
            block.get("kind") == "tool_use"
            for block in pending_assistant["blocks"]
        )
        yield pending_assistant


def session_exists(session_id: str) -> bool:
    """会话库中是否存在该 session（不读取内容）。"""
    db_path = transcripts_db_path()
    if not db_path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                (str(session_id or ""),),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def session_transcript(
    session_id: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any] | None:
    """展开一个会话的回合流（分页）；会话不存在返回 None。

    返回的 turns 按会话时间序展开，``sequence`` 是回合在整段会话中的
    序号（不是本页内偏移），供前端拼页与定位。
    """
    db_path = transcripts_db_path()
    if not db_path.is_file():
        return None
    requested_offset = max(0, int(offset))
    page_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (str(session_id or ""),),
            ).fetchone()
            total_messages = int((total_row or [0])[0])
            if total_messages <= 0:
                return None
            rows = conn.execute(
                """
                SELECT role, content_json, created_at FROM messages
                WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (str(session_id or ""),),
            )
            window: list[dict[str, Any]] = []
            observed_turns = 0
            truncated = False
            for turn in _iter_session_turns(rows):
                observed_turns = int(turn["sequence"]) + 1
                if int(turn["sequence"]) < requested_offset:
                    continue
                if len(window) >= page_limit:
                    truncated = True
                    break
                window.append(turn)
    except sqlite3.Error:
        return None
    # 为判断 truncated 只多读一个回合；未到会话末尾时不扫描余下消息，
    # 因而 total_turns 暂未知。到末页后 observed_turns 即为准确总数。
    total_turns = None if truncated else observed_turns
    return {
        "format": "turns-v1",
        "session_id": str(session_id or ""),
        "total_messages": total_messages,
        "total_turns": total_turns,
        "offset": requested_offset,
        "returned": len(window),
        "truncated": truncated,
        "next_offset": requested_offset + len(window),
        "turns": window,
    }


def session_raw_messages(
    session_id: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any] | None:
    """分页读取未裁剪的原始 message；内部推理 block 仍被移除。"""
    db_path = transcripts_db_path()
    if not db_path.is_file():
        return None
    requested_offset = max(0, int(offset))
    page_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (str(session_id or ""),),
            ).fetchone()
            total_messages = int((total_row or [0])[0])
            if total_messages <= 0:
                return None
            rows = conn.execute(
                """
                SELECT id, role, content_json, created_at, token_count
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at, id
                LIMIT ? OFFSET ?
                """,
                (str(session_id or ""), page_limit, requested_offset),
            ).fetchall()
    except sqlite3.Error:
        return None
    messages = [
        {
            "sequence": requested_offset + index,
            "message_id": int(row[0]),
            "role": str(row[1] or ""),
            "created_at": str(row[3] or ""),
            "token_count": int(row[4] or 0),
            "content": _untrimmed_message_content(row[2]),
        }
        for index, row in enumerate(rows)
    ]
    next_offset = requested_offset + len(messages)
    return {
        "format": "raw-messages-v1",
        "session_id": str(session_id or ""),
        "total_messages": total_messages,
        "offset": requested_offset,
        "returned": len(messages),
        "truncated": next_offset < total_messages,
        "next_offset": next_offset,
        "omitted_block_types": sorted(_SKIPPED_BLOCK_TYPES),
        "messages": messages,
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "session_exists",
    "session_raw_messages",
    "session_transcript",
    "transcripts_db_path",
]
