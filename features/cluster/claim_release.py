"""Elevated local force release of a fenced standalone AOA operation."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def force_release_local_aoa_operation(repository, device_key: str, local_worker_id: str) -> tuple[bool, str]:
    """Called only behind the elevated administrator force-release endpoint.

    Same-host AOA validates the shared claim before every USB transfer. Remote
    executors use Worker-local fences, so their active operations cannot be
    released here. Jobs, reservations and borrowed claims retain their guards.
    """
    claim = repository.claims.active_claim(device_key)
    if not claim:
        return True, "设备未被占用"
    source = str(claim.get("source_id") or "")
    if (claim.get("worker_id") != local_worker_id
            or claim.get("source_type") != "cluster-device-action"
            or not source.startswith("operation:")):
        return False, "该占用不是本机独立回放，请先结束对应任务或预约"
    command = repository.get_command_by_operation(local_worker_id, source.removeprefix("operation:"))
    payload = (command or {}).get("payload") or {}
    tokens = payload.get("lease_tokens") or []
    matching = any(token.get("device_id") == device_key
                   and token.get("lease_id") == claim["id"]
                   and token.get("generation") == claim["generation"] for token in tokens)
    if (not command or command.get("command_type") != "device_action" or command.get("job_id")
            or payload.get("claim_source_id") != source
            or payload.get("release_claim_on_terminal") is not True or not matching):
        return False, "无法确认占用对应的独立操作，请先结束原操作后重试"
    terminal = {"completed", "failed", "cancelled"}
    if command["status"] not in terminal:
        if (command["status"] not in {"queued", "delivered", "accepted", "running"}
                or command.get("delivered_at") or command.get("acknowledged_at")):
            return False, "该回放由 Worker 执行，请先通过回放停止按钮结束操作"
        if payload.get("action") != "aoa_sequence" or len(payload.get("devices") or []) != 1:
            return False, "设备仍在执行其他操作，请等待该操作结束"
        # Persist cancellation before revoking the shared fence. A running
        # executor observes cancel or fencing loss; late ACKs cannot resurrect
        # this terminal receipt. Revocation may interrupt input: require review.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload.update(aoa_control_action="cancel",
                       aoa_control_revision=int(payload.get("aoa_control_revision") or 0) + 1)
        with repository._lock, repository.connect() as conn:
            conn.execute(
                """UPDATE cluster_commands SET status='cancelled',payload_json=?,result_json=?,
                   error=?,updated_at=?,acknowledged_at=? WHERE id=? AND status IN
                   ('queued','delivered','accepted','running')""",
                (json.dumps(payload, separators=(",", ":")),
                 json.dumps({"success": False, "forced_release": True, "reconcile_required": True}),
                 "管理员强制终止 AOA 回放；请核对设备当前页面后再操作", now, now, command["id"]),
            )
    released = repository.claims.release_operation_if_current(
        device_key, claim["id"], claim["generation"], source)
    if not released:
        return False, "设备占用已变化，请刷新后重试"
    return True, "原回放占用及 USB 端口锁已释放；请核对设备当前页面后再操作"
