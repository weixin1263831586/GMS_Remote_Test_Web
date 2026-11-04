"""Route local AOA force release through its persisted command and fences."""
from foundation.cluster_port import get_cluster_service

from .locks import device_lock_manager


def force_release_device_lock(device_id: str) -> tuple[bool, str | None]:
    worker_id = device_lock_manager.local_worker_id
    serial = device_id.removeprefix(f"{worker_id}:")
    key = f"{worker_id}:{serial}"
    claim = device_lock_manager.registry.active_claim(key)
    if claim and claim.get("source_type") == "cluster-device-action":
        try:
            cluster = get_cluster_service()
        except RuntimeError:
            return False, "回放服务暂不可用，请恢复服务后再释放"
        if cluster.config.local_worker_id != worker_id:
            return False, "本机 Worker 配置已变化，请刷新后重试"
        return cluster.repository.force_release_local_aoa_operation(key, worker_id)
    return device_lock_manager.force_unlock_device(serial)
