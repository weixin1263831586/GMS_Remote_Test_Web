from __future__ import annotations

from enum import Enum
import re

from pydantic import BaseModel, Field, field_validator


USBIP_BUSID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _validated_usbip_busids(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in values or []:
        busid = str(raw or "").strip()
        if not USBIP_BUSID_PATTERN.fullmatch(busid):
            raise ValueError(f"invalid USB/IP busid: {busid}")
        if busid not in normalized:
            normalized.append(busid)
    return normalized


class ADBForwardStartRequest(BaseModel):
    device_host: str = ""


class USBIPStartRequest(BaseModel):
    device_host: str | None = None
    worker_id: str = ""
    busids: list[str] = Field(default_factory=list, max_length=32)
    device_password: str | None = Field(
        default="",
        description="设备主机SSH密码",
    )
    manual_connect: bool = Field(
        default=False,
        description="用户显式点击连接",
    )

    _validate_busids = field_validator("busids")(_validated_usbip_busids)


class USBIPDisconnectRequest(BaseModel):
    device_host: str | None = None
    source_host: str = ""
    worker_id: str = ""
    busids: list[str] = Field(default_factory=list, max_length=32)

    _validate_busids = field_validator("busids")(_validated_usbip_busids)


class DeviceLockRequest(BaseModel):
    device_id: str | None = None
    devices: list[str] | None = None
    action: str = "lock"


class DeviceActionRequest(BaseModel):
    devices: list[str] = Field(..., description="设备ID列表")


class WifiConnectRequest(DeviceActionRequest):
    ssid: str = ""
    password: str = ""


class DeviceShellRequest(BaseModel):
    serial_no: str = Field(..., description="设备序列号")


class VerifiedBootState(str, Enum):
    LOCKED = "green"
    UNLOCKED_ORANGE = "orange"
    UNLOCKED_YELLOW = "yellow"

    @property
    def is_locked(self) -> bool:
        return self == self.LOCKED

    @property
    def display_text(self) -> str:
        return {
            "green": "已锁定 (GREEN)",
            "orange": "未锁定 (ORANGE)",
            "yellow": "未锁定 (YELLOW)",
        }[self.value]
