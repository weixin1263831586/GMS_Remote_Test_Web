"""
设备相关数据模型
"""
from pydantic import BaseModel, Field
from typing import List, Optional


class DeviceInfo(BaseModel):
    """设备信息"""
    device_id: str = Field(..., description="设备ID")
    serial_no: Optional[str] = None
    model: Optional[str] = None
    android_version: Optional[str] = None
    status: Optional[str] = None
    locked: bool = False
    locked_by: Optional[str] = None
    locked_by_self: bool = False
    source_type: Optional[str] = None  # 'local' or 'usbip'
    source_host: Optional[str] = None


class DeviceLockRequest(BaseModel):
    """设备锁定请求"""
    devices: List[str] = Field(..., description="设备ID列表")
    action: str = Field(default="lock", description="操作: lock or unlock")


class DeviceRebootRequest(BaseModel):
    """设备重启请求"""
    devices: List[str] = Field(..., description="设备ID列表")


class DeviceRemountRequest(BaseModel):
    """设备remount请求"""
    devices: List[str] = Field(..., description="设备ID列表")


class DeviceWifiRequest(BaseModel):
    """设备WiFi连接请求"""
    devices: List[str] = Field(..., description="设备ID列表")
    ssid: str = Field(default="AndroidWifi", description="WiFi名称")
    password: str = Field(default="1234567890", description="WiFi密码")


class DeviceScreenRequest(BaseModel):
    """设备屏幕显示请求"""
    devices: List[str] = Field(..., description="设备ID列表")


class DeviceInfoRequest(BaseModel):
    """设备信息查询请求"""
    devices: List[str] = Field(..., description="设备ID列表")
