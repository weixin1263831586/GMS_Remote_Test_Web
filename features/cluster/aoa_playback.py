"""Owner-bound controls for one persisted AOA playback."""
from __future__ import annotations

from foundation.error_model import ApiError

from .api import _require_cluster_enabled, service


async def control_aoa_playback(control_id: str, worker_id: str, owner_id: str, action: str) -> dict:
    cluster = service()
    _require_cluster_enabled(remote=worker_id != cluster.config.local_worker_id)
    command, revision = cluster.repository.request_aoa_playback_control(
        worker_id, control_id, owner_id, action)
    if not command:
        raise ApiError.not_found("playback operation not found or already finished")
    action = command["payload"]["aoa_control_action"]
    if worker_id != cluster.config.local_worker_id:
        cluster.repository.create_command({
            "worker_id": worker_id, "command_type": "aoa_playback_control",
            "operation_id": f"{command['id']}:control:{revision}",
            "payload": {"control_id": control_id, "action": action,
                        "revision": revision, "owner_id": owner_id,
                        "target_command_id": command["id"]},
        })
    return {"success": True, "accepted": True, "action": action,
            "command_id": command["id"], "control_revision": revision}
