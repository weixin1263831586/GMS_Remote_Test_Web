from __future__ import annotations

from features.devices import (
    broadcast_device_lock_update,
    device_lock_manager,
    release_device_locks,
)


async def acquire_test_devices(
    *,
    client_id: str,
    username: str,
    devices: list[str],
) -> tuple[list[str], list[dict[str, str]]]:
    locked = []
    failed = []
    for device_id in devices:
        success, message = device_lock_manager.lock_device(
            device_id,
            client_id,
            username,
        )
        if success:
            locked.append(device_id)
        else:
            failed.append({"device_id": device_id, "error": message})

    if failed:
        await release_device_locks(
            client_id,
            locked,
            broadcast=False,
        )
        return [], failed

    await broadcast_device_lock_update(locked)
    return locked, []


async def release_test_devices(
    client_id: str,
    devices: list[str],
) -> None:
    await release_device_locks(client_id, devices)
