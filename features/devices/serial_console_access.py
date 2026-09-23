"""Authorization, device availability, and writer fencing for serial consoles."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from features.auth import CurrentUser, auth_service
from foundation.cluster_port import get_local_worker_id
from foundation.error_model import ApiError
from foundation.security_audit import security_audit_logger

from .locks import device_lock_manager


def _is_agent(connection: Any) -> bool:
    return getattr(connection.state, "auth_method", None) == "agent_token"


def _binding_identity(port: dict[str, Any]) -> tuple[str, str]:
    binding = port.get("binding") or {}
    return (
        str(binding.get("worker_id") or "").strip(),
        str(binding.get("device_id") or "").strip(),
    )


def agent_can_read_port(request: Request, port: dict[str, Any]) -> bool:
    if not _is_agent(request):
        return True
    worker_id, device_id = _binding_identity(port)
    if not worker_id or not device_id:
        return False
    record = getattr(request.state, "agent_token_record", None)
    return auth_service.agent_acl_allows(
        record, "workers", worker_id
    ) and auth_service.agent_acl_allows(record, "devices", device_id)


def visible_ports(request: Request, ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [port for port in ports if agent_can_read_port(request, port)]


def require_visible_port(
    request: Request, ports: list[dict[str, Any]], port_key: str
) -> dict[str, Any]:
    port = next(
        (item for item in ports if str(item.get("port_key") or "") == port_key),
        None,
    )
    if port is None or not agent_can_read_port(request, port):
        raise ApiError.not_found("serial console port not found")
    return port


def device_serial_availability(
    ports: list[dict[str, Any]], *, device_id: str, worker_id: str
) -> dict[str, Any]:
    local_worker = get_local_worker_id()
    if worker_id != local_worker:
        return {
            "device_id": device_id,
            "worker_id": worker_id,
            "scope": "controller-local",
            "available": False,
            "state": "unsupported_worker",
            "confidence": "none",
            "ports": [],
            "candidates": [],
            "reason": "当前串口服务仅管理 Controller 本机端口；该设备属于其他 Worker。",
            "next_actions": [
                {"action": "在设备所在 Worker 部署串口采集能力并显式绑定端口"}
            ],
        }

    matches = [
        port
        for port in ports
        if _binding_identity(port) == (worker_id, device_id)
    ]
    candidates = [
        _candidate(port)
        for port in ports
        if port.get("online") and not (port.get("binding") or {}).get("device_id")
    ]
    if not matches:
        return {
            "device_id": device_id,
            "worker_id": worker_id,
            "scope": "controller-local",
            "available": False,
            "state": "unbound",
            "confidence": "none",
            "ports": [],
            "candidates": candidates,
            "reason": "没有与该设备显式绑定的串口；USB-UART 与 ADB 序列号无法可靠自动关联。",
            "next_actions": [
                {"action": "由人工核对线缆后，在设备串口页完成绑定"}
            ],
        }

    summaries = [_port_availability(port) for port in matches]
    available = any(item["available"] for item in summaries)
    states = {item["state"] for item in summaries}
    state = (
        "active" if "active" in states else "ready" if "ready" in states
        else "error" if "error" in states else "offline"
    )
    output_verified = any(item["output_verified"] for item in summaries)
    open_verified = any(item["capture_active"] for item in summaries)
    if output_verified:
        confidence = "output_verified"
        reason = "串口已绑定、已成功打开，并已观察到设备输出。"
        next_actions: list[dict[str, str]] = []
    elif open_verified:
        confidence = "open_verified"
        reason = "串口已绑定且已成功打开，但尚未观察到设备输出。"
        next_actions = [{"action": "触发设备启动或串口输出，并复核波特率"}]
    elif available:
        confidence = "explicit_binding"
        reason = "串口已显式绑定且设备节点在线，但尚未验证能否打开或收到输出。"
        next_actions = [{"action": "开启采集以验证端口可打开，并触发设备输出"}]
    else:
        confidence = "explicit_binding"
        reason = "串口已绑定，但端口离线或打开失败。"
        next_actions = [{"action": "检查接线、驱动、端口占用和 dialout 权限"}]
    return {
        "device_id": device_id,
        "worker_id": worker_id,
        "scope": "controller-local",
        "available": available,
        "state": state,
        "confidence": confidence,
        "ports": summaries,
        "candidates": candidates,
        "reason": reason,
        "next_actions": next_actions,
    }


def _candidate(port: dict[str, Any]) -> dict[str, Any]:
    return {
        key: port.get(key)
        for key in (
            "port_key",
            "devname",
            "by_id",
            "usb_path",
            "vendor_product",
            "driver",
            "identity_source",
            "identity_stable",
        )
    }


def _port_availability(port: dict[str, Any]) -> dict[str, Any]:
    online = bool(port.get("online"))
    active = bool(port.get("capture_active"))
    error = str(port.get("error") or "")
    available = online and (active or not error)
    state = "active" if active else "ready" if available else "error" if error else "offline"
    return {
        **_candidate(port),
        "online": online,
        "available": available,
        "state": state,
        "capture_enabled": bool(port.get("capture_enabled")),
        "capture_active": active,
        "last_output_at": str(port.get("last_output_at") or ""),
        "output_verified": bool(port.get("last_output_at")),
        "error": error,
    }


@dataclass
class SerialWriterClaim:
    source_id: str
    device_id: str
    device_key: str
    owner_id: str


def acquire_writer_claim(
    user: CurrentUser | None, port: dict[str, Any]
) -> SerialWriterClaim:
    binding = port.get("binding") or {}
    device_id = str(binding.get("device_id") or "").strip()
    worker_id = str(binding.get("worker_id") or "").strip()
    if not binding.get("identity_verified") or not device_id or not worker_id:
        raise RuntimeError("请重新绑定设备身份后再写入串口")
    if worker_id != get_local_worker_id():
        raise RuntimeError("远端 Worker 串口暂不支持交互写入")
    owner_id = user.resource_owner_id if user else "serial-console-dev"
    username = user.username if user else "anonymous-dev"
    source_id = f"serial-console:{uuid.uuid4().hex}"
    acquired, records = device_lock_manager.lock_devices(
        [device_id],
        owner_id,
        username,
        source_id=source_id,
        source_type="local-serial-console",
        ttl_seconds=120,
        allow_existing_source=False,
    )
    if not acquired:
        holder = str((records[0] if records else {}).get("source_type") or "active operation")
        raise RuntimeError(f"设备正被 {holder} 占用，串口保持只读")
    record = records[0]
    claim = SerialWriterClaim(source_id, device_id, record["device_key"], owner_id)
    audit_console_event(user, "serial_writer_acquired", port, status="ok")
    return claim


def renew_writer_claim(claim: SerialWriterClaim) -> bool:
    return bool(
        device_lock_manager.registry.renew(
            claim.source_id, 120, device_keys=[claim.device_key]
        )
    )


def release_writer_claim(
    claim: SerialWriterClaim | None, user: CurrentUser | None, port: dict[str, Any] | None
) -> None:
    if claim is None:
        return
    device_lock_manager.registry.release(claim.source_id)
    audit_console_event(user, "serial_writer_released", port or {}, status="ok")


def audit_console_event(
    user: CurrentUser | None,
    action: str,
    port: dict[str, Any],
    *,
    status: str,
    byte_count: int = 0,
) -> None:
    if user is None:
        return
    worker_id, device_id = _binding_identity(port)
    security_audit_logger.log_event(
        {
            "action_type": "device_serial_console",
            "source": "web",
            "operation": action,
            "status": status,
            "actor_id": user.actor_id if user else "anonymous-dev",
            "owner_id": user.resource_owner_id if user else "anonymous-dev",
            "username": user.username if user else "anonymous-dev",
            "worker_id": worker_id,
            "device_id": device_id,
            "port_key": str(port.get("port_key") or ""),
            "byte_count": max(0, int(byte_count)),
        }
    )


__all__ = [
    "SerialWriterClaim",
    "acquire_writer_claim",
    "agent_can_read_port",
    "audit_console_event",
    "device_serial_availability",
    "release_writer_claim",
    "renew_writer_claim",
    "require_visible_port",
    "visible_ports",
]
