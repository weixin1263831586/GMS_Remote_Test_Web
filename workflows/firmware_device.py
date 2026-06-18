from __future__ import annotations

from fastapi.responses import JSONResponse

from features.devices.locks import device_lock_manager
from features.devices.support import (
    broadcast_device_lock_update,
    release_device_locks,
)


async def lock_firmware_devices(
    *,
    client_id: str,
    username: str,
    devices: list[str],
    error_prefix: str = "Devices occupied",
):
    locked_devices = []
    failed_devices = []
    for device_id in devices:
        success, message = device_lock_manager.lock_device(
            device_id,
            client_id,
            username,
        )
        if success:
            locked_devices.append(device_id)
        else:
            failed_devices.append(
                {"device_id": device_id, "error": message}
            )

    if failed_devices:
        await release_device_locks(
            client_id,
            locked_devices,
            broadcast=False,
        )
        error_message = f"{error_prefix}:\n" + "\n".join(
            f"- {item['device_id']} ({item['error']})"
            for item in failed_devices
        )
        return [], JSONResponse(
            content={
                "success": False,
                "error": error_message,
                "failed_devices": failed_devices,
            },
            status_code=409,
        )

    await broadcast_device_lock_update(locked_devices)
    return locked_devices, None
