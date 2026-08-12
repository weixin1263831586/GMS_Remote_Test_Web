from __future__ import annotations

import functools
import inspect
import threading
from collections.abc import Callable
from typing import Any

from foundation.responses import error_response


class USBIPOperationGate:
    """Reject overlapping mutations for the same physical USB/IP source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()

    @staticmethod
    def normalize_scope(value: object) -> str:
        return str(value or "__default__").strip().casefold() or "__default__"

    def try_acquire(self, scope: object) -> bool:
        key = self.normalize_scope(scope)
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    def release(self, scope: object) -> None:
        with self._lock:
            self._active.discard(self.normalize_scope(scope))


usbip_operation_gate = USBIPOperationGate()


def _request_scope(signature, args, kwargs) -> str:
    bound = signature.bind_partial(*args, **kwargs)
    request_model = bound.arguments.get("req")
    explicit_host = getattr(request_model, "device_host", "")
    if explicit_host:
        return str(explicit_host)
    request = bound.arguments.get("request")
    query_host = getattr(getattr(request, "query_params", None), "get", lambda _key: "")(
        "device_host"
    )
    return str(query_host or "__default__")


def serialize_usbip_operation(
    function: Callable[..., Any] | None = None,
    *,
    gate: USBIPOperationGate | None = None,
):
    """FastAPI-safe decorator that rejects duplicate connect/disconnect work."""

    selected_gate = gate or usbip_operation_gate

    def decorate(target):
        signature = inspect.signature(target)

        @functools.wraps(target)
        async def guarded(*args, **kwargs):
            scope = _request_scope(signature, args, kwargs)
            if not selected_gate.try_acquire(scope):
                return error_response(
                    "该USB/IP来源正在执行连接或断开操作，请等待完成后重试",
                    status_code=409,
                    error_code="USBIP_OPERATION_IN_PROGRESS",
                    retryable=True,
                    device_host="" if scope == "__default__" else scope,
                )
            try:
                return await target(*args, **kwargs)
            finally:
                selected_gate.release(scope)

        return guarded

    return decorate(function) if function is not None else decorate


def usbip_error_fields(message: str) -> dict[str, object]:
    detail = str(message or "")
    lowered = detail.lower()
    rules = (
        (("ssh", "凭据"), "USBIP_SOURCE_SSH_FAILED", "请检查来源主机SSH地址、凭据和sshd服务。"),
        (("usbipd未安装",), "USBIPD_NOT_INSTALLED", "请先在Windows来源主机安装usbipd-win。"),
        (("未找到android设备", "未找到android usb", "设备已不可用"), "USBIP_SOURCE_DEVICE_NOT_FOUND", "请刷新USB设备列表并确认设备仍连接来源主机。"),
        (("绑定失败", "bind failed"), "USBIP_BIND_FAILED", "请检查usbipd共享状态和Windows管理员权限。"),
        (("vhci",), "VHCI_LOAD_FAILED", "请在目标Linux主机安装USB/IP工具并加载vhci_hcd。"),
        (("attach",), "USBIP_ATTACH_FAILED", "请检查TCP 3240、防火墙及残留USB/IP会话。"),
        (("unauthorized",), "ADB_UNAUTHORIZED", "请在设备端确认ADB授权。"),
        (("offline",), "ADB_OFFLINE", "请检查USB链路并等待ADB重新枚举。"),
        (("回滚", "cleanup"), "ROLLBACK_INCOMPLETE", "请使用当前接入中的“清理”操作完成残留会话清理。"),
    )
    for markers, code, remediation in rules:
        if any(marker in lowered for marker in markers):
            return {
                "error_code": code,
                "retryable": code != "USBIPD_NOT_INSTALLED",
                "remediation": remediation,
            }
    return {
        "error_code": "USBIP_OPERATION_FAILED",
        "retryable": True,
        "remediation": "请查看USB/IP分层状态和来源/目标主机日志后重试。",
    }


def selected_usbip_serials(
    assignments: dict[str, dict],
    device_host: str,
    busids: list[str],
) -> list[str]:
    selected_busids = {str(item or "").strip() for item in busids if str(item or "").strip()}
    return list(dict.fromkeys(
        str(serial or "").strip()
        for assignment in assignments.values()
        if str(assignment.get("device_host") or "") == device_host
        and str(assignment.get("busid") or "") in selected_busids
        for serial in assignment.get("device_serials") or []
        if str(serial or "").strip()
    ))


def has_remaining_usbip_assignments(
    assignments: dict[str, dict],
    device_host: str,
    removed_busids: list[str],
) -> bool:
    removed = {str(item or "").strip() for item in removed_busids}
    return any(
        str(item.get("device_host") or "") == device_host
        and str(item.get("busid") or "") not in removed
        and str(item.get("status") or "") in {
            "attaching", "attached", "unknown", "cleanup_required", "detaching",
        }
        for item in assignments.values()
    )
