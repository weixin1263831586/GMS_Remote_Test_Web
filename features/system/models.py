from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VNCStartRequest(BaseModel):
    worker_id: str | None = None
    # 请求可携带该字段，但服务端不信任其内容。
    host: str | None = None
    password: str | None = None
    vnc_password: str | None = None
    force_restart: bool = False


class VPNConnectRequest(BaseModel):
    vpn_name: str | None = None


class NotificationCreateRequest(BaseModel):
    title: str = Field(..., max_length=120)
    message: str = Field(default="", max_length=600)
    level: str = Field(default="info", max_length=20)
    category: str = Field(default="system", max_length=50)
    data: dict[str, Any] | None = None


class NotificationReadRequest(BaseModel):
    ids: list[str] | None = None


class SecurityPageViewRequest(BaseModel):
    page: str = Field(..., max_length=80)
    title: str | None = Field(default="", max_length=160)
    hash: str | None = Field(default="", max_length=160)
