"""Pydantic request/response schemas used by FastAPI routes."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClientInfoRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    ip: Optional[str] = None


class DeviceLockRequest(BaseModel):
    device_id: Optional[str] = None
    devices: Optional[List[str]] = None
    action: str = 'lock'


class TestStartRequest(BaseModel):
    test_type: str = ""
    test_module: str = ""
    test_case: str = ""
    retry_dir: str = ""
    test_suite: str = ""
    local_server: str = ""
    devices: List[str] = Field(default_factory=list)
    client_id: str = "test_client"


class DeviceActionRequest(BaseModel):
    devices: List[str] = Field(..., description="设备ID列表")


class WifiConnectRequest(DeviceActionRequest):
    ssid: str = "AndroidWifi"
    password: str = "1234567890"


class VNCStartRequest(BaseModel):
    host: Optional[str] = None
    password: Optional[str] = None
    vnc_password: Optional[str] = None


class ADBForwardStartRequest(BaseModel):
    device_host: str
    device_password: Optional[str] = Field(default="", description="设备主机SSH密码")


class USBIPStartRequest(BaseModel):
    device_host: Optional[str] = None
    device_password: Optional[str] = Field(default="", description="设备主机SSH密码")


class USBIPDisconnectRequest(BaseModel):
    device_host: Optional[str] = None


class VPNConnectRequest(BaseModel):
    vpn_name: Optional[str] = None


class SNBurnRequest(BaseModel):
    devices: List[str]
    sn_code: str


class NotificationCreateRequest(BaseModel):
    title: str = Field(..., max_length=120)
    message: str = Field(default="", max_length=600)
    level: str = Field(default="info", max_length=20)
    category: str = Field(default="system", max_length=50)
    data: Optional[Dict[str, Any]] = None


class NotificationReadRequest(BaseModel):
    ids: Optional[List[str]] = None


class SecurityPageViewRequest(BaseModel):
    page: str = Field(..., max_length=80)
    title: Optional[str] = Field(default="", max_length=160)
    hash: Optional[str] = Field(default="", max_length=160)


class DeviceShellRequest(BaseModel):
    serial_no: str = Field(..., description="设备序列号")


class TestParseArgsRequest(BaseModel):
    params: List[str] = Field(default_factory=list, description="命令行参数列表")


class TestParseArgsResponse(BaseModel):
    success: bool = True
    device: str = ""
    test_type: str = ""
    test_module: str = ""
    test_case: str = ""
    test_suite: str = ""
    retry_dir: str = ""
    warnings: List[str] = Field(default_factory=list)
    help_text: str = ""


class SuiteApkAnalyzeRequest(BaseModel):
    suite_path: str
    path: str


class TradefedListResultsRequest(BaseModel):
    suite_path: str
    tradefed_bin: Optional[str] = None


class TestSuiteDownloadRequest(BaseModel):
    url: str = Field(..., description="测试套件下载地址")
    save_dir: Optional[str] = Field(default=None, description="保存目录（默认：~/GMS-Suite）")


class TestSuiteExtractRequest(BaseModel):
    archive_path: str = Field(..., description="压缩包文件路径")
    extract_dir: Optional[str] = Field(default=None, description="解压目录（默认：~/GMS-Suite）")
    target_dir_name: Optional[str] = Field(default=None, description="解压后的文件夹名称")


class TestSuiteAddLocalRequest(BaseModel):
    path: str = Field(..., description="本地测试套件路径")
