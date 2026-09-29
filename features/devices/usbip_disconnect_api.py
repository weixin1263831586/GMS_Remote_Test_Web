"""USB/IP disconnect route group.

从 integrations_api.py 拆出（2026-09 大文件收敛）。
包含 /api/usbip/disconnect 路由（stop_usbip）。"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin
from foundation.responses import error_response

from . import runtime
from .adb_forward_api import (
    start_adb_forward as start_adb_forward,
)
from .adb_forward_api import (
    stop_adb_forward as stop_adb_forward,
)
from .locks import device_lock_manager
from .models import USBIPDisconnectRequest
from .support import (
    DeviceSSHConnection,
    acquire_device_operation_claim,
    audit_device_operation,
    format_device_list_info,
    notify_device_change,
    release_device_operation_claim,
)
from .usbip import find_device_host_password, usbip_manager
from .usbip_assignments import (
    load_usbip_assignments as _usbip_assignments,
)
from .usbip_assignments import (
    mark_usbip_detach_unknown as _mark_usbip_detach_unknown,
)
from .usbip_assignments import (
    next_transport_generation as _next_transport_generation,
)
from .usbip_assignments import (
    save_usbip_assignments as _save_usbip_assignments,
)
from .usbip_assignments import (
    usbip_assignment_key as _usbip_assignment_key,
)
from .usbip_linux_source import stop_ubuntu_usbip_server
from .usbip_operations import (
    has_remaining_usbip_assignments,
    selected_usbip_serials,
    serialize_usbip_operation,
)
from .usbip_persistence import (
    usbip_assignment_lock as _usbip_assignment_lock,
)
from .usbip_support import _clear_usbip_device_sources as _clear_usbip_device_sources
from .usbip_support import _detach_ubuntu_usbip_for_devices as _detach_ubuntu_usbip_for_devices
from .usbip_support import _invalidate_device_cache as _invalidate_device_cache
from .usbip_support import _local_worker_id as _local_worker_id
from .usbip_support import _mark_usbip_source_disconnected as _mark_usbip_source_disconnected
from .usbip_support import _resolve_usbip_device_host as _resolve_usbip_device_host
from .usbip_support import _usbip_devices_for_host as _usbip_devices_for_host
from .usbip_support import _usbip_remote_host as _usbip_remote_host
from .usbip_support import _wait_for_adb_devices_removed as _wait_for_adb_devices_removed


logger = logging.getLogger(__name__)


router = APIRouter()

# ==================== USB/IP Disconnect ====================

@router.post("/api/usbip/disconnect")
@serialize_usbip_operation
async def stop_usbip(
    request: Request,
    req: USBIPDisconnectRequest | None = Body(default=None),
    _elevated=Depends(require_elevated_admin),
):
    """Stop USB/IP forwarding (supports specifying host).

    Sensitive: disconnects/removes device forwarding, so requires admin elevation.
    """
    config = runtime.config_manager.load_config()
    client_id = runtime.get_client_id_from_request(request)
    tailscale_mode = False

    if req and req.worker_id:
        from foundation.cluster_port import get_cluster_service
        from foundation.cluster_port import run_worker_command as _run_worker_command

        try:
            cluster = get_cluster_service()
        except (AttributeError, RuntimeError) as exc:
            logger.warning("[USB/IP Stop] cluster service unavailable: %s", exc)
            return error_response(
                "集群服务未初始化，无法执行远端 USB/IP 断开",
                status_code=503,
            )
        if req.worker_id != cluster.config.local_worker_id:
            # Cleanup must remain possible after cluster mode is disabled;
            # otherwise a persisted/physical remote attachment becomes
            # impossible to detach from the UI. New remote attaches still
            # require cluster mode in start_usbip().
            if not req.device_host or not req.busids:
                return error_response(
                    "远端 Worker 断开需要 device_host 和 busids", status_code=400
                )
            worker = cluster.repository.get_worker(req.worker_id) or {}
            if worker.get("status") not in {"online", "busy"}:
                return error_response("worker is not online", status_code=409)
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                selected_assignments = []
                invalid_assignments = []
                for busid in req.busids:
                    current = assignments.get(
                        _usbip_assignment_key(req.device_host, busid)
                    ) or {}
                    if (
                        current.get("worker_id") != req.worker_id
                        or current.get("status") in {"attaching", "detaching"}
                    ):
                        invalid_assignments.append(busid)
                    else:
                        selected_assignments.append(current)
                disconnect_generation = _next_transport_generation(assignments)
                disconnect_operation_id = f"usbip-detach-{uuid.uuid4().hex}"
            if invalid_assignments:
                return error_response(
                    "USB/IP分配状态已变化，请刷新后重试: "
                    + ", ".join(invalid_assignments),
                    status_code=409,
                )
            claimed_serials = list(dict.fromkeys(
                str(serial or "").strip()
                for assignment in selected_assignments
                for serial in assignment.get("device_serials") or []
                if str(serial or "").strip()
            ))
            # Legacy/pending assignments may not yet have a BUSID→ADB serial
            # mapping. Do not claim every device on the Worker in that case:
            # those devices are unrelated to this physical USB/IP port and the
            # broad claim can block an otherwise safe cleanup detach.
            claim_source = ""
            if claimed_serials:
                operation_id = disconnect_operation_id
                claim_source = f"operation:{operation_id}"
                try:
                    claim_records = (
                        cluster.repository.acquire_device_operation_claim(
                            req.worker_id,
                            claimed_serials,
                            # ADR 0010: canonical resource-owner accessor.
                            owner_id=_elevated.resource_owner_id,
                            source_type="cluster-usbip",
                            source_id=claim_source,
                            ttl_seconds=10 * 60,
                            username=_elevated.username,
                        )
                    )
                except ValueError as exc:
                    return error_response(str(exc), status_code=409)
                lease_tokens = cluster.repository.claim_fencing_tokens(
                    claim_records, operation_id
                )
            else:
                lease_tokens = []
            source_host = req.source_host or _usbip_remote_host(req.device_host)
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                for busid in req.busids:
                    key = _usbip_assignment_key(req.device_host, busid)
                    current = assignments.get(key) or {}
                    if current.get("worker_id") == req.worker_id:
                        current.update({
                            "status": "detaching",
                            "generation": disconnect_generation,
                            "operation_id": disconnect_operation_id,
                            "timestamp": time.time(),
                        })
                        assignments[key] = current
                _save_usbip_assignments(assignments)
            command_payload = {
                "device_host": req.device_host,
                "source_host": source_host,
                "busids": req.busids,
                "devices": claimed_serials,
                "lease_tokens": lease_tokens,
                "claim_source_id": claim_source,
                "release_claim_on_terminal": bool(claim_source),
                "generation": disconnect_generation,
                "operation_id": disconnect_operation_id,
            }
            try:
                result = await _run_worker_command(
                    req.worker_id,
                    "usbip_detach",
                    command_payload,
                    timeout=90,
                )
            except HTTPException as exc:
                _mark_usbip_detach_unknown(
                    req.device_host, req.busids, req.worker_id,
                    disconnect_generation,
                )
                if claim_source and exc.status_code != 504:
                    cluster.repository.claims.release(
                        claim_source, status="failed"
                    )
                raise
            except Exception:
                _mark_usbip_detach_unknown(
                    req.device_host, req.busids, req.worker_id,
                    disconnect_generation,
                )
                if claim_source:
                    cluster.repository.claims.release(
                        claim_source, status="failed"
                    )
                raise
            already_detached = bool(result.get("already_detached"))
            if not result.get("detached_ports") and not already_detached:
                _mark_usbip_detach_unknown(
                    req.device_host, req.busids, req.worker_id,
                    disconnect_generation,
                )
                return error_response(
                    f"{req.worker_id} 未确认USB/IP设备已断开，保留分配记录",
                    status_code=502,
                )
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                for busid in req.busids:
                    key = _usbip_assignment_key(req.device_host, busid)
                    current = assignments.get(key) or {}
                    if current.get("worker_id") == req.worker_id:
                        assignments.pop(key, None)
                _save_usbip_assignments(assignments)
            # The detach command already returned the Worker's settled device
            # list. Apply it immediately so the UI reflects the removal without
            # waiting for the next heartbeat (~15s lag).
            if "devices" in result:
                try:
                    cluster.repository.refresh_worker_devices(
                        req.worker_id, result.get("devices") or []
                    )
                except Exception as exc:
                    logger.warning(
                        "[USB/IP Stop] failed to refresh worker devices for %s: %s",
                        req.worker_id,
                        exc,
                    )
            return JSONResponse(content={
                "success": True,
                "device_host": req.device_host,
                "worker_id": req.worker_id,
                "busids": req.busids,
                "removed_devices": claimed_serials,
                "message": f"已从 {req.worker_id} 断开USB/IP设备",
                **result,
            })

    if req and req.device_host:
        config["device_host"] = req.device_host
    else:
        tunnel_host, tunnel_usbip_host = runtime.resolve_tailscale_device_host(request, client_id)
        if tunnel_host:
            config["device_host"] = tunnel_host
            config["usbip_attach_host"] = tunnel_usbip_host
            tailscale_mode = True
        else:
            config["device_host"] = _resolve_usbip_device_host(request, config)

    device_password = find_device_host_password(config["device_host"], config)
    if not device_password:
        device_password = config.get("device_pswd", "")
    if device_password:
        config["device_pswd"] = device_password

    devices_to_remove: list[str] = []
    usbip_attach_host = config.get("usbip_attach_host")
    ubuntu_detached_ports: list[str] = []
    remaining_devices_after_detach: list[str] = []
    claim_source_id = ""
    claim_records: list[dict] = []
    has_remaining_assignments = False

    try:
        from features.devices.reconnect import (
            stop_usbip_reconnect_for_host,
            suppress_usbip_reconnect,
        )

        devices_to_remove = _usbip_devices_for_host(config["device_host"])
        selected_busids = list(req.busids) if req and req.busids else []
        assignments_before_disconnect = _usbip_assignments()
        if selected_busids:
            devices_to_remove = selected_usbip_serials(
                assignments_before_disconnect,
                config["device_host"],
                selected_busids,
            )
        has_remaining_assignments = has_remaining_usbip_assignments(
            assignments_before_disconnect,
            config["device_host"],
            selected_busids,
        ) if selected_busids else False
        if not devices_to_remove and device_lock_manager.get_all_locks():
            return JSONResponse(
                content={
                    "success": False,
                    "error": (
                        "USB/IP inventory is incomplete while device leases are active; "
                        "disconnect was refused"
                    ),
                },
                status_code=409,
            )
        claim_source_id, claim_records, conflict = acquire_device_operation_claim(
            request,
            devices_to_remove,
            "usbip-disconnect",
        )
        if conflict:
            return conflict
        suppress_usbip_reconnect(config["device_host"], devices_to_remove)
        stop_usbip_reconnect_for_host(config["device_host"], timeout=2)

        ubuntu_ssh = runtime.ssh_manager.get_connection(config)
        if ubuntu_ssh:
            try:
                # 同步 SSH 往返 + settle sleep，放线程池避免冻结事件循环
                # （同函数下方 _detach_source_bindings 同理）。
                detach_result = await asyncio.to_thread(
                    _detach_ubuntu_usbip_for_devices,
                    ubuntu_ssh,
                    device_host=config["device_host"],
                    usbip_attach_host=usbip_attach_host,
                    devices_to_remove=devices_to_remove,
                    busids=(req.busids if req else None),
                    detach_all=tailscale_mode,
                    settle=tailscale_mode,
                )
                ubuntu_detached_ports = list(detach_result["detached_ports"])
                remaining_devices_after_detach = list(detach_result["remaining_devices"])
                runtime.ssh_manager.return_connection(ubuntu_ssh)
            except Exception as e:
                ubuntu_ssh.close()
                logger.warning(f"[USB/IP Stop] detach Ubuntu usbip ports failed: {e}")

        if tailscale_mode:
            logger.info("[USB/IP Stop] Public mode keeps source-side usbipd bindings; only local attach is detached")
            await asyncio.sleep(1)
            _clear_usbip_device_sources(config["device_host"], devices_to_remove)
        else:
            # usbipd detach/unbind 是同步 SSH 调用，放线程池避免
            # 冻结事件循环（DeviceSSHConnection 的建立本身也是阻塞的）。
            def _detach_source_bindings():
                with DeviceSSHConnection(config) as source_ssh:
                    if source_ssh is None:
                        return
                    if usbip_manager._detect_source_os(source_ssh) == "linux":
                        if selected_busids and has_remaining_assignments:
                            logger.info(
                                "[USB/IP Stop] Ubuntu source %s still exports devices for other assignments; usbipd kept running",
                                config["device_host"],
                            )
                            return
                        stop_result = stop_ubuntu_usbip_server(
                            runtime.ssh_manager, source_ssh,
                        )
                        if not stop_result.get("success"):
                            logger.warning(
                                "[USB/IP Stop] Failed to stop Ubuntu usbipd on %s: %s",
                                config["device_host"],
                                stop_result.get("detail"),
                            )
                        return
                    if selected_busids:
                        for busid in selected_busids:
                            runtime.ssh_manager.execute_command(
                                source_ssh,
                                f"usbipd detach --busid {shlex.quote(busid)}",
                                timeout=10,
                            )
                    else:
                        runtime.ssh_manager.execute_command(
                            source_ssh, "usbipd unbind --all", timeout=10
                        )

            await asyncio.to_thread(_detach_source_bindings)
            await asyncio.sleep(2)

            _clear_usbip_device_sources(config["device_host"], devices_to_remove)
            if remaining_devices_after_detach:
                verification_ssh = runtime.ssh_manager.get_connection(config)
                if verification_ssh:
                    try:
                        remaining_devices_after_detach = sorted(
                            # ADB 轮询等待含 sleep 循环 + SSH 往返，放线程池。
                            await asyncio.to_thread(
                                _wait_for_adb_devices_removed,
                                verification_ssh,
                                set(remaining_devices_after_detach),
                            )
                        )
                    finally:
                        runtime.ssh_manager.return_connection(verification_ssh)

        _mark_usbip_source_disconnected(
            config["device_host"],
            has_remaining_assignments=has_remaining_assignments,
        )

        # 失效缓存，避免返回已断开的 USB/IP 设备。
        _invalidate_device_cache()

        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")
        if remaining_devices_after_detach:
            logger.warning(
                "[USB/IP Stop] Devices still visible after detach cleanup: %s",
                remaining_devices_after_detach,
            )

        if req and req.worker_id and req.busids:
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                for busid in req.busids:
                    key = _usbip_assignment_key(config["device_host"], busid)
                    current = assignments.get(key) or {}
                    if current.get("worker_id") == req.worker_id:
                        assignments.pop(key, None)
                _save_usbip_assignments(assignments)

        await notify_device_change(devices_to_remove, "USB/IP Stop")

        response = JSONResponse(content={
            "success": True,
            "message": f"Local devices disconnected{disconnected_devices_info}",
            "detached_ports": ubuntu_detached_ports,
            "removed_devices": devices_to_remove,
            "remaining_devices": remaining_devices_after_detach,
        })
        audit_device_operation(
            request,
            "usbip-disconnect",
            claim_records,
            response.status_code,
        )
        return response

    except HTTPException:
        # Windows 不可连接时仅清理连接和设备来源状态。
        if not devices_to_remove:
            devices_to_remove = _usbip_devices_for_host(config["device_host"])
        try:
            from features.devices.reconnect import suppress_usbip_reconnect
            suppress_usbip_reconnect(config["device_host"], devices_to_remove)
        except Exception:
            pass
        _clear_usbip_device_sources(config["device_host"], devices_to_remove)

        _mark_usbip_source_disconnected(
            config["device_host"],
            has_remaining_assignments=has_remaining_assignments,
        )

        # 失效缓存，避免返回已断开的 USB/IP 设备。
        _invalidate_device_cache()

        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")

        await notify_device_change(devices_to_remove, "USB/IP Stop")

        response = JSONResponse(content={
            "success": True,
            "message": f"Local devices disconnected{disconnected_devices_info}",
            "removed_devices": devices_to_remove,
        })
        audit_device_operation(
            request,
            "usbip-disconnect",
            claim_records,
            response.status_code,
        )
        return response
    except Exception as exc:
        if claim_records:
            audit_device_operation(
                request,
                "usbip-disconnect",
                claim_records,
                500,
                error=str(exc),
            )
        raise
    finally:
        release_device_operation_claim(claim_source_id)


__all__ = [
    "stop_usbip",
]
