"""Bounded model retries and private, session-bound planning checkpoints."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field

from features.assistant import kkagent_bridge


MODEL_TIMEOUT_SECONDS = 120
TURN_TIMEOUT_SECONDS = 300
CHECKPOINT_TTL_SECONDS = 900
MAX_CHECKPOINTS = 128


@dataclass
class PlanningState:
    message: str
    observations: list = field(default_factory=list)
    completed: dict = field(default_factory=dict)
    rounds: int = 0
    progress_id: str = ""
    automatic_retry_used: bool = False


# Keep operational state out of the JSON session/history payload.
_checkpoints: dict[str, tuple[str, float, str, dict, PlanningState]] = {}


def _api():
    from features.assistant import api

    return api


def discard(session_id: str) -> None:
    _checkpoints.pop(session_id, None)


def restore(session: dict, retry_id: str) -> PlanningState:
    saved = _checkpoints.get(session["session_id"])
    if (not saved or saved[0] != retry_id or saved[1] <= time.monotonic()
            or saved[2] != str(session.get("owner_id") or "")
            or saved[3] != (session.get("workspace_context") or {})):
        raise ValueError("本轮重试已过期或工作范围已改变，请重新描述目标。")
    discard(session["session_id"])
    session.pop("agent_retry", None)
    state = saved[4]
    state.progress_id = ""
    state.automatic_retry_used = False
    return state


def progress(session: dict, state: PlanningState, status: str, detail: str, *, waiting=False) -> None:
    api = _api()
    if not state.progress_id:
        api._append_step(session, "Agent 规划", status, detail)
        state.progress_id = session["steps"][-1]["id"]
    # display_record replaces records when appending; find the current copy.
    step = next((item for item in session["steps"] if item["id"] == state.progress_id), None)
    if step:
        step.update(status=status, detail=detail, data={
            "kind": "agent_model_wait" if waiting else "agent_planning",
            "started_at": api._now_iso(),
        })
        session["updated_at"] = api._now_iso()


async def infer(session: dict, state: PlanningState, context: str):
    bridge = session.setdefault("kkagent_bridge", {})
    retrying = False
    while True:
        detail = ("模型响应超时，正在自动重试（1/1）" if retrying
                  else f"正在规划第 {state.rounds + 1} 步，等待模型响应")
        progress(session, state, "running", detail, waiting=True)
        session["agent_backend"] = {"mode": "kkagent", "state": "retrying" if retrying else "thinking"}
        result = await kkagent_bridge.run_turn(
            state.message, session_id=str(bridge.get("kkagent_session_id") or ""),
            context=context, timeout_seconds=MODEL_TIMEOUT_SECONDS,
        )
        if result.session_id:
            bridge["kkagent_session_id"] = result.session_id
        if session.get("status") == "cancelled":
            return result
        if result.ok or result.error_code != "timeout" or state.automatic_retry_used:
            progress(session, state, "running", "模型已响应，正在处理规划结果")
            return result
        # Retry inference only. Controller tool dispatch remains outside this loop.
        state.automatic_retry_used = True
        retrying = True


def fail(session: dict, state: PlanningState, detail: str, error_code: str = "failed") -> None:
    now = time.monotonic()
    for sid, saved in list(_checkpoints.items()):
        if saved[1] <= now:
            discard(sid)
    if len(_checkpoints) >= MAX_CHECKPOINTS:
        discard(next(iter(_checkpoints)))
    retry_id = uuid.uuid4().hex
    _checkpoints[session["session_id"]] = (
        retry_id, now + CHECKPOINT_TTL_SECONDS, str(session.get("owner_id") or ""),
        deepcopy(session.get("workspace_context") or {}), state,
    )
    session["agent_retry"] = {"id": retry_id}
    session["agent_backend"] = {"mode": "kkagent", "state": "timeout" if error_code == "timeout" else "failed", "detail": detail}
    progress(session, state, "warning", detail + "；已保留本轮进度，可重试")
    _api()._append_message(
        session, "assistant", detail + "。本轮规划尚未完成，已保留查询结果和操作回执，点击下方按钮继续。",
        data={"quick_actions": [{"label": "重试本轮规划", "action": "agent_retry", "params": {"retry_id": retry_id}}]},
    )


def operation_key(tool_name: str, params: dict) -> str:
    encoded = json.dumps([tool_name, params], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
