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
    source_id = f"firmware:{client_id}"
    locked, records = device_lock_manager.lock_devices(
        devices,
        client_id,
        username,
        source_id=source_id,
        source_type="firmware",
        allow_existing_source=False,
    )
    if not locked:
        failed_devices = [
            {
                "device_id": item.get("serial") or item.get("device_key"),
                "error": (
                    f"设备已被 {item.get('username') or item.get('owner_id')} "
                    f"的 {item.get('source_type') or 'operation'} 占用"
                ),
            }
            for item in records
        ]
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

    await broadcast_device_lock_update(devices)
    return list(dict.fromkeys(devices)), None


async def release_firmware_devices(
    client_id: str,
    devices: list[str],
) -> None:
    await release_device_locks(
        client_id,
        devices,
        source_id=f"firmware:{client_id}",
    )
