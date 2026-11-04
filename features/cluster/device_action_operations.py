"""Request binding for persistent device actions and local execution receipts."""
from __future__ import annotations

import hashlib
import json
import logging

from foundation.error_model import ApiError


logger = logging.getLogger(__name__)


def request_digest(worker_id: str, devices: list[str], payload: dict, owner_id: str) -> str:
    canonical = json.dumps({"worker_id": worker_id, "devices": devices,
                            "payload": payload, "owner_id": owner_id},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def require_same_request(command: dict, owner_id: str, digest: str) -> None:
    payload = command.get("payload") or {}
    if payload.get("owner_id") != owner_id:
        raise ApiError.not_found("device action operation not found")
    if payload.get("request_sha256") != digest:
        raise ApiError.conflict("operation key is already bound to a different request")


def fenced_local_payload(repository, payload: dict) -> dict:
    """Revalidate local AOA claims throughout calibration and playback."""
    tokens = tuple(dict(token) for token in payload.get("lease_tokens") or [])

    def validate_claims():
        if not tokens:
            raise RuntimeError("AOA requires an active device claim")
        for token in tokens:
            current = repository.claims.active_claim(token["device_id"])
            if (not current or current["id"] != token["lease_id"]
                    or current["generation"] != token["generation"]):
                raise RuntimeError("device claim changed during AOA; reconcile required")

    validate_claims()
    return {**payload, "_validate_fencing": validate_claims}


def _local_aoa_progress_reporter(repository, command: dict):
    """本机 durable_local ``aoa_sequence`` 的逐条进度上报。

    远端 Worker 经 HTTP ``POST /workers/{id}/commands/{id}/events`` 上报
    （worker_agent/app.py 的 CommandEventUploader）；本机执行与 repository
    同进程，直接 append（同 UNIQUE(command_id,sequence) 幂等去重），事件
    字段与远端路径完全一致，保证浏览器对两种执行路径的高亮行为相同。
    进度上报失败绝不打断回放：命令终态仍由 ack 收敛兜底。
    """
    worker_id = command["worker_id"]
    command_id = command["id"]

    sequence = 0

    def report_event(event: dict) -> None:
        nonlocal sequence
        sequence += 1
        index, total = event.get("index", 0), event.get("total", 0)
        text = str(event.get("command") or "")
        try:
            repository.append_command_events(worker_id, command_id, [{
                "sequence": sequence, "event_type": "progress", "source": "aoa", "level": "info",
                "message": f"{index}/{total} {event.get('phase', '')} {text}"[:500],
                "payload": {**event, "command": text[:1000]},
            }])
        except Exception:
            logger.warning("local AOA progress write failed for %s", command_id, exc_info=True)

    def report(index: int, total: int, text: str) -> None:
        report_event({"phase": "completed", "index": index, "total": total, "command": text})

    return report, report_event


def execute_local_operation(repository, command: dict, execute) -> None:
    """The running receipt is committed before the first physical input.

    A lost response only follows this receipt. An interrupted process must
    never restart a running HID sequence from the first command.
    """
    payload = command["payload"]
    try:
        payload = fenced_local_payload(repository, payload)
        report, report_event = (_local_aoa_progress_reporter(repository, command)
                                if payload.get("action") == "aoa_sequence" else (None, None))
        if payload.get("playback_control_id"):
            def control_source():
                current = repository.get_command(command["id"])
                if not current:
                    raise RuntimeError("AOA operation disappeared; stop playback")
                state = current.get("payload") or {}
                return {"action": state.get("aoa_control_action"),
                        "revision": state.get("aoa_control_revision", 0)}

            payload = {**payload, "_aoa_control_source": control_source}
        result = execute(payload["action"], payload["devices"], payload,
                         progress_callback=report, progress_event_callback=report_event)
        repository.ack_command(command["worker_id"], command["id"], {
            "status": "completed", "result": result,
        })
    except Exception as exc:
        repository.ack_command(command["worker_id"], command["id"], {
            "status": "cancelled" if getattr(exc, "cancelled", False) else "failed", "error": str(exc),
            "result": {"success": False, "reconcile_required": not getattr(exc, "cancelled", False)},
        })
    finally:
        if payload.get("release_claim_on_terminal"):
            repository.claims.release(payload["claim_source_id"])
