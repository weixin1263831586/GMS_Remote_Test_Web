"""Read back one analysis session's full kkagent transcript.

「查看分析」弹框的「完整会话」数据源：把 Daily Brief 单条分析对应的
kkagent 会话（``~/.kkagent/transcripts.db`` 的 messages 表）按序展开成
事件流，供 UI 回放取证过程。

安全边界（硬性）：
- session_id 一律来自该 owner 该 issue 的 ``redmine_daily_brief_ai_executions``
  记录（``ai_execution.session_id``），API 不接受调用方传入的任意 session id；
  不存在归属关系的会话一律 404，不泄露存在性。
- 内容裁剪后再下发：跳过模型内部 thinking；工具输出/输入只保留有限预览，
  防止单条日志把响应与浏览器内存撑爆。
- 会话内容是分析证据，只读展示；本模块不做任何写操作。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


# 单条工具输出的预览上限：足够看清关键报错，又不会把弹框渲染成日志页。
TOOL_OUTPUT_PREVIEW_CHARS = 2000
TOOL_INPUT_PREVIEW_CHARS = 600
TEXT_PREVIEW_CHARS = 4000
# 单页事件数：浏览器渲染上限优先，分页按钮负责翻页。
DEFAULT_PAGE_SIZE = 80
MAX_PAGE_SIZE = 200

_SKIPPED_BLOCK_TYPES = {"thinking"}
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


def _render_blocks(blocks: Any, role: str, sequence: int) -> list[dict[str, Any]]:
    """把一条 message 的 content blocks 展开为回放事件。"""
    events: list[dict[str, Any]] = []
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            blocks = [{"type": "text", "text": str(blocks)}]
    if not isinstance(blocks, list):
        return events
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in _SKIPPED_BLOCK_TYPES:
            continue
        if block_type not in _RENDERABLE_BLOCK_TYPES:
            continue
        event: dict[str, Any] = {
            "sequence": sequence,
            "role": role,
            "kind": block_type,
        }
        if block_type == "text":
            event["text"] = _clip(block.get("text"), TEXT_PREVIEW_CHARS)
        elif block_type == "tool_use":
            event["tool_name"] = str(block.get("name") or "")
            event["tool_input"] = _clip(block.get("input"), TOOL_INPUT_PREVIEW_CHARS)
        elif block_type == "tool_result":
            event["tool_call_id"] = str(block.get("tool_use_id") or "")
            event["is_error"] = bool(block.get("is_error"))
            event["output"] = _clip(block.get("content"), TOOL_OUTPUT_PREVIEW_CHARS)
        events.append(event)
        sequence += 1
    return events


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
    """展开一个会话的回放事件（分页）；会话不存在返回 None。

    返回的 events 按会话时间序展开，``sequence`` 是事件在整段会话中的
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
                SELECT role, content_json FROM messages
                WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (str(session_id or ""),),
            )
            window: list[dict[str, Any]] = []
            sequence = 0
            truncated = False
            for role, content_json in rows:
                rendered = _render_blocks(content_json, str(role or ""), sequence)
                sequence += len(rendered)
                for event in rendered:
                    if int(event["sequence"]) < requested_offset:
                        continue
                    if len(window) >= page_limit:
                        truncated = True
                        break
                    window.append(event)
                if truncated:
                    break
    except sqlite3.Error:
        return None
    # 为判断 truncated 只多读一个事件；未到会话末尾时不扫描余下消息，
    # 因而 total_events 暂未知。到末页后 sequence 即为准确总事件数。
    total_events = None if truncated else sequence
    return {
        "session_id": str(session_id or ""),
        "total_messages": total_messages,
        "total_events": total_events,
        "offset": requested_offset,
        "returned": len(window),
        "truncated": truncated,
        "next_offset": requested_offset + len(window),
        "events": window,
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "session_exists",
    "session_transcript",
    "transcripts_db_path",
]
