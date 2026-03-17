"""
配置相关数据模型
"""
from pydantic import BaseModel, Field
from typing import Optional, List


class ConfigUpdate(BaseModel):
    """配置更新请求"""
    ubuntu_host: Optional[str] = None
    ubuntu_user: Optional[str] = None
    ubuntu_pswd: Optional[str] = None
    device_host: Optional[str] = None
    device_pswd: Optional[str] = None
    suites_path: Optional[str] = None
    scrcpy_path: Optional[str] = None
    usbip_vid_pid: Optional[str] = None
    device_hosts: Optional[List[dict]] = None
    client_hosts: Optional[dict] = None
    client_ssh_credentials: Optional[List[dict]] = None


class ClientInfoRequest(BaseModel):
    """客户端信息请求"""
    username: Optional[str] = None
    ip: Optional[str] = None


class ClientDetectRequest(BaseModel):
    """客户端检测请求"""
    ip: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
