from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ADBForwardStartRequest(BaseModel):
    device_host: str
    device_password: str | None = Field(
        default="",
        description="设备主机SSH密码",
    )


class USBIPStartRequest(BaseModel):
    device_host: str | None = None
    device_password: str | None = Field(
        default="",
        description="设备主机SSH密码",
    )
    manual_connect: bool = Field(
        default=False,
        description="用户显式点击连接",
    )


class USBIPDisconnectRequest(BaseModel):
    device_host: str | None = None


class DeviceLockRequest(BaseModel):
    device_id: str | None = None
    devices: list[str] | None = None
    action: str = "lock"


class DeviceActionRequest(BaseModel):
    devices: list[str] = Field(..., description="设备ID列表")


class WifiConnectRequest(DeviceActionRequest):
    ssid: str = "AndroidWifi"
    password: str = "1234567890"


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
