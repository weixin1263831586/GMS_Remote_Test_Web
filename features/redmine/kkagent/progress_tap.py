"""stream-json 事件 → 分析进度 sink 的归一化分流器（评审 L2+）。

从 consume_line 的 on_event 回调接入：只消费结构化执行事件
（tool_call/tool_result/llm_retry），不外显 reasoning/assistant 文本；
tool input 原样转发给 sink——sink 侧（AnalysisProgressRecorder）负责
按身份字段 allowlist 脱敏与截断。

``session`` 事件单独分流：kkagent 流一输出 session_id 就转发给
sink.session_available()（若 sink 支持），让「会话回放」在分析进行中
即可 tail 同一 session，而不用等分析结束后 ai_execution 落库。
"""

from __future__ import annotations

import time
from typing import Any


class ProgressTap:
    """一次 kkagent 流的 tool_call/tool_result → 进度事件适配。"""

    def __init__(self, sink: Any):
        self.sink = sink
        self._started: dict[str, float] = {}
        self._inputs: dict[str, Any] = {}
        self._session_reported = False

    def on_event(self, event: dict[str, Any]) -> None:
        if self.sink is None:
            return
        event_type = str(event.get("type") or "")
        if event_type == "session":
            if not self._session_reported:
                session_id = str(event.get("session_id") or "")
                if session_id:
                    self._session_reported = True
                    reporter = getattr(self.sink, "session_available", None)
                    if callable(reporter):
                        reporter(session_id)
            return
        if event_type == "tool_call":
            call_id = str(event.get("tool_call_id") or "")
            self._started[call_id] = time.monotonic()
            self._inputs[call_id] = event.get("input")
            self.sink.tool_started(str(event.get("tool_name") or ""), event.get("input"))
        elif event_type == "tool_result":
            call_id = str(event.get("tool_call_id") or "")
            started = self._started.pop(call_id, None)
            tool_input = self._inputs.pop(call_id, None)
            duration_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
            self.sink.tool_finished(
                str(event.get("tool_name") or ""), tool_input,
                ok=not bool(event.get("is_error")), duration_ms=duration_ms,
            )
        elif event_type == "llm_retry":
            self.sink.progress("模型服务暂时不稳定，已自动重试")


__all__ = ["ProgressTap"]
