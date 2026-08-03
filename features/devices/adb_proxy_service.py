from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

from fastapi import HTTPException

from worker_agent.adb_proxy import ADB_PROXY_VERSION

from .adb_proxy_security import create_pair_grant


# A successful source_start/target_connect command is reflected by the Worker
# heartbeat asynchronously (normally every 15 seconds).  During that gap the
# most recent observation still describes the previous proxy generation.
ADB_PROXY_RECONCILE_GRACE_SECONDS = 35


class ADBProxyService:
    """Coordinate adbproxy-rs without changing canonical device ownership."""

    def __init__(self) -> None:
        self.config_manager: Any = None
        self._lock = threading.RLock()
        # adb-hub and the persisted assignment map are shared resources.
        # Serialize connect/add/disconnect workflows so concurrent requests
        # cannot restart the same Hub from stale snapshots or lose devices.
        self._operation_lock = asyncio.Lock()
        self._observations: dict[str, dict[str, Any]] = {}

    def observe_worker(self, worker_id: str, status: dict[str, Any]) -> None:
        """Record the latest Worker-owned process state for reconciliation."""
        if not worker_id:
            return
        observation = dict(status or {})
        observation["observed_at"] = time.time()
        with self._lock:
            self._observations[str(worker_id)] = observation

    def _observation(self, worker_id: str) -> dict[str, Any]:
        with self._lock:
            value = dict(self._observations.get(str(worker_id)) or {})
        if not value or time.time() - float(value.get("observed_at") or 0) > 45:
            return {}
        return value

    def assignments(self) -> dict[str, dict[str, Any]]:
        if self.config_manager is None:
            return {}
        runtime = self.config_manager.get_runtime_config() or {}
        values = runtime.get("adb_proxy_assignments") or {}
        return dict(values) if isinstance(values, dict) else {}

    def save_assignments(self, assignments: dict[str, dict[str, Any]]) -> None:
        if self.config_manager is None:
            raise RuntimeError("ADB Proxy service is not configured")
        saved = self.config_manager.update_runtime_config({
            "adb_proxy_assignments": assignments,
        })
        if not saved:
            raise RuntimeError("无法保存ADB Proxy接入状态")

    def usbip_assignments(self) -> dict[str, dict[str, Any]]:
        """Read USB/IP routes from the same injected runtime config.

        Keeping this lookup on the service avoids coupling ADB Proxy checks to
        the process-global devices runtime.  It also guarantees that status
        validation and assignment writes observe the same config backend.
        """
        if self.config_manager is None:
            return {}
        runtime = self.config_manager.get_runtime_config() or {}
        values = runtime.get("usbip_cluster_assignments") or {}
        return dict(values) if isinstance(values, dict) else {}

    def status(self) -> dict[str, Any]:
        from features.cluster import get_cluster_service

        cluster = get_cluster_service()
        workers = cluster.list_workers()
        if not cluster.effective_enabled:
            workers = [
                item for item in workers
                if (
                    item.get("id") == cluster.config.local_worker_id
                    or bool(
                        (item.get("capabilities") or {}).get(
                            "adb_proxy_source_only"
                        )
                    )
                )
            ]
        devices_by_worker: dict[str, list[dict[str, Any]]] = {}
        for device in cluster.repository.list_devices():
            devices_by_worker.setdefault(str(device.get("worker_id") or ""), []).append({
                "serial": str(device.get("serial") or ""),
                "state": str(device.get("state") or ""),
                "transport": str(device.get("transport") or ""),
                "model": str((device.get("properties") or {}).get("model") or ""),
            })
        hosts = []
        for worker in workers:
            capabilities = worker.get("capabilities") or {}
            proxy_installed = bool(capabilities.get("adb_proxy"))
            proxy_version = str(capabilities.get("adb_proxy_version") or "")
            if worker.get("id") == cluster.config.local_worker_id:
                from worker_agent.adb_proxy import capability_status

                local_capability = capability_status()
                proxy_installed = bool(local_capability.get("installed"))
                proxy_version = str(local_capability.get("version") or "")
            hosts.append({
                "worker_id": worker.get("id", ""),
                "name": worker.get("name") or worker.get("id", ""),
                "hostname": worker.get("hostname", ""),
                "address": worker.get("address") or worker.get("hostname") or "",
                "status": worker.get("status", "offline"),
                "adb_proxy": (
                    proxy_installed
                    and self._compatible_proxy_version(proxy_version)
                ),
                "adb_proxy_version": proxy_version,
                "adb_proxy_source_only": bool(
                    capabilities.get("adb_proxy_source_only")
                ),
                "devices": devices_by_worker.get(str(worker.get("id") or ""), []),
            })
        host_status = {
            str(item.get("worker_id") or ""): str(item.get("status") or "")
            for item in hosts
        }
        assignments = []
        for stored in self.assignments().values():
            assignment = dict(stored)
            source_worker_id = str(assignment.get("source_worker_id") or "")
            target_worker_id = str(assignment.get("target_worker_id") or "")
            source_online = host_status.get(
                source_worker_id
            ) in {"online", "busy"}
            target_online = host_status.get(
                target_worker_id
            ) in {"online", "busy"}
            if not source_online or not target_online:
                assignment["status"] = "host_offline"
            else:
                assignment["status"] = self._reconciled_status(
                    assignment,
                    devices_by_worker.get(target_worker_id, []),
                )
            assignment["health"] = {
                "source": self._observation(source_worker_id),
                "target": self._observation(target_worker_id),
            }
            assignments.append(assignment)
        from .transport_registry import build_transport_records

        return {
            "success": True,
            "version": ADB_PROXY_VERSION,
            "cluster_enabled": cluster.effective_enabled,
            "local_worker_id": cluster.config.local_worker_id,
            "hosts": hosts,
            "assignments": assignments,
            "transport_records": build_transport_records(
                assignments,
                list(self.usbip_assignments().values()),
            ),
            "connected": any(
                item.get("status") == "connected" for item in assignments
            ),
        }

    def _reconciled_status(
        self,
        assignment: dict[str, Any],
        target_devices: list[dict[str, Any]],
    ) -> str:
        stored_status = str(assignment.get("status") or "unknown")
        if stored_status == "disconnect_failed":
            return stored_status
        source_id = str(assignment.get("source_worker_id") or "")
        target_id = str(assignment.get("target_worker_id") or "")
        source = self._observation(source_id)
        target = self._observation(target_id)
        if not source or not target:
            return (
                stored_status
                if stored_status in {"connecting", "connect_failed"}
                else "recovering"
            )
        expected = {str(item) for item in assignment.get("devices") or []}
        generation = int(assignment.get("generation") or 0)
        grace_status = (
            "recovering" if self._in_reconciliation_grace(assignment) else ""
        )
        source_state = source.get("source") or {}
        if not source.get("proxy_running") or not source_state.get("running"):
            return grace_status or "degraded_source"
        if generation and int(source_state.get("generation") or 0) != generation:
            return grace_status or "degraded_source"
        if not expected.issubset({str(item) for item in source_state.get("devices") or []}):
            return grace_status or "degraded_source"
        imports = (target.get("target") or {}).get("imports") or []
        target_import = next((
            item for item in imports
            if str(item.get("source_worker_id") or "") == source_id
        ), None)
        if not target.get("hub_running") or not target_import:
            return grace_status or "degraded_target"
        if generation and int(target_import.get("generation") or 0) != generation:
            return grace_status or "degraded_target"
        if not expected.issubset({str(item) for item in target_import.get("devices") or []}):
            return grace_status or "degraded_target"
        visible = {
            str(item.get("serial") or "").split(":")[-1]
            for item in target_devices
            if str(item.get("state") or "") not in {"offline", "unknown"}
        }
        if expected and not expected.issubset(visible):
            return grace_status or "device_missing"
        return "connected"

    @staticmethod
    def _in_reconciliation_grace(assignment: dict[str, Any]) -> bool:
        """Treat post-command heartbeat lag as convergence, not a failure."""
        if str(assignment.get("status") or "") not in {"connecting", "connected"}:
            return False
        try:
            age = time.time() - float(assignment.get("updated_at") or 0)
        except (TypeError, ValueError):
            return False
        return 0 <= age <= ADB_PROXY_RECONCILE_GRACE_SECONDS

    async def connect(
        self,
        source_worker_id: str,
        target_worker_id: str,
        devices: list[str],
    ) -> dict[str, Any]:
        async with self._operation_lock:
            return await self._connect(
                source_worker_id,
                target_worker_id,
                devices,
            )

    async def logs(self, worker_id: str) -> dict[str, Any]:
        """Fetch bounded, sanitized executor logs from one Worker on demand."""
        from features.cluster.api import _run_worker_command

        worker = self._online_proxy_worker(worker_id)
        capabilities = worker.get("capabilities") or {}
        if not capabilities.get("adb_proxy_logs"):
            return self._log_summary_fallback(
                worker_id,
                "该 Worker 版本尚不支持远程日志读取，请重新部署 Worker 后查看完整日志。",
            )
        try:
            result = await _run_worker_command(
                worker_id,
                "adb_proxy",
                {"action": "logs"},
                timeout=20,
            )
        except HTTPException as exc:
            detail = str(exc.detail or "")
            if exc.status_code == 502 and "unsupported adb-proxy action: logs" in detail:
                return self._log_summary_fallback(
                    worker_id,
                    "该 Worker 尚未加载日志接口，请重新部署或重启 Worker 后重试。",
                )
            raise
        return {
            "success": True,
            "worker_id": worker_id,
            "supported": True,
            "proxy": list(result.get("proxy") or [])[-100:],
            "hub": list(result.get("hub") or [])[-100:],
        }

    def _log_summary_fallback(self, worker_id: str, notice: str) -> dict[str, Any]:
        """Keep rolling deployments diagnosable without turning the UI into a 502."""
        observation = self._observation(worker_id)
        recent = observation.get("recent_errors") or {}
        return {
            "success": True,
            "worker_id": worker_id,
            "supported": False,
            "notice": notice,
            "proxy": [str(recent.get("proxy") or "")]
            if recent.get("proxy") else [],
            "hub": [str(recent.get("hub") or "")]
            if recent.get("hub") else [],
        }

    async def _connect(
        self,
        source_worker_id: str,
        target_worker_id: str,
        devices: list[str],
    ) -> dict[str, Any]:
        from features.cluster import get_cluster_service
        from features.cluster.api import _require_cluster_enabled, _run_worker_command

        cluster = get_cluster_service()
        local_worker_id = cluster.config.local_worker_id
        if source_worker_id == target_worker_id:
            raise HTTPException(400, "设备来源和接入主机不能相同")
        source = self._online_proxy_worker(source_worker_id)
        target = self._online_proxy_worker(target_worker_id)
        self._require_idle_target(target_worker_id, target)
        source_only = bool(
            (source.get("capabilities") or {}).get("adb_proxy_source_only")
        )
        target_source_only = bool(
            (target.get("capabilities") or {}).get("adb_proxy_source_only")
        )
        if target_source_only:
            raise HTTPException(
                409,
                f"{target_worker_id} 是仅来源主机，不能作为ADB接入目标",
            )
        if (
            target_worker_id != local_worker_id
            or (source_worker_id != local_worker_id and not source_only)
        ):
            _require_cluster_enabled(remote=True)
        source_address = str(
            source.get("address") or source.get("hostname") or ""
        ).strip()
        target_address = str(
            target.get("address") or target.get("hostname") or ""
        ).strip()
        if source_address.lower() in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(
                409, f"{source_worker_id} 没有可供其他主机连接的内网/VPN地址"
            )
        selected = list(dict.fromkeys(str(item or "").strip() for item in devices))
        if not selected:
            raise HTTPException(400, "请至少选择一个ADB设备")
        key = self._assignment_key(source_worker_id, target_worker_id)
        with self._lock:
            current_assignments = self.assignments()
            previous_assignment = dict(current_assignments.get(key) or {})
        previous_devices = list(dict.fromkeys(
            str(item or "").strip()
            for item in previous_assignment.get("devices") or []
            if str(item or "").strip()
        ))
        additions = [serial for serial in selected if serial not in previous_devices]
        if previous_assignment and not additions:
            raise HTTPException(409, "所选ADB设备已全部接入该目标主机")
        requested_devices = [*previous_devices, *additions]
        usbip_assignments = [
            item for item in self.usbip_assignments().values()
            if str(item.get("worker_id") or "") == target_worker_id
            and item.get("status") in {
                "attaching", "attached", "unknown", "cleanup_required",
            }
        ]
        usbip_statuses = {
            str(item.get("status") or "") for item in usbip_assignments
        }
        if usbip_statuses & {"unknown", "cleanup_required"}:
            raise HTTPException(
                409,
                f"{target_worker_id} 存在待确认或待清理的USB/IP分配，"
                "请先在USB/IP当前接入中断开并清理",
            )
        if "attaching" in usbip_statuses:
            raise HTTPException(
                409,
                f"{target_worker_id} 正在接入USB/IP设备，请稍后重试ADB接入",
            )
        usbip_serials = {
            str(serial or "").strip()
            for item in usbip_assignments
            for serial in item.get("device_serials") or []
            if str(serial or "").strip()
        }
        usbip_conflicts = sorted(set(additions) & usbip_serials)
        if usbip_conflicts:
            raise HTTPException(
                409,
                "接入主机已有同序列号USB/IP设备，请先断开USB/IP接入: "
                + ", ".join(usbip_conflicts),
            )
        assigned_proxy_serials = {
            str(serial or "").strip()
            for current_key, item in self.assignments().items()
            if current_key != key
            and str(item.get("target_worker_id") or "") == target_worker_id
            for serial in item.get("devices") or []
            if str(serial or "").strip()
        }
        proxy_conflicts = sorted(set(additions) & assigned_proxy_serials)
        if proxy_conflicts:
            raise HTTPException(
                409,
                "接入主机的其他ADB Proxy来源已有同序列号设备: "
                + ", ".join(proxy_conflicts),
            )
        source_devices = {
            str(item.get("serial") or ""): item
            for item in cluster.repository.list_devices(source_worker_id)
        }
        invalid = [
            serial for serial in additions
            if serial not in source_devices
            or source_devices[serial].get("state") != "available"
        ]
        if invalid:
            raise HTTPException(
                409, "来源设备当前不可执行ADB操作: " + ", ".join(invalid)
            )
        target_serials = {
            str(item.get("serial") or "")
            for item in cluster.repository.list_devices(target_worker_id)
            if str(item.get("transport") or "") != "adb_proxy"
        }
        conflicts = sorted(set(additions) & target_serials)
        if conflicts:
            raise HTTPException(
                409, "接入主机存在同序列号设备: " + ", ".join(conflicts)
            )

        grant = create_pair_grant(
            source_worker_id,
            target_worker_id,
            local_worker_id,
        )
        with self._lock:
            assignments = self.assignments()
            for current_key, current in assignments.items():
                if current_key == key:
                    continue
                if current.get("source_worker_id") == source_worker_id:
                    raise HTTPException(409, "该设备来源已接入其他主机")
                if current.get("target_worker_id") == source_worker_id:
                    raise HTTPException(
                        409, "设备来源主机当前正在聚合其他ADB来源"
                    )
                if current.get("source_worker_id") == target_worker_id:
                    raise HTTPException(
                        409, "接入主机当前正在对外提供ADB Proxy"
                    )
            generation = max(
                int(time.time() * 1000),
                int(previous_assignment.get("generation") or 0) + 1,
            )
            operation_id = f"adb-proxy-{uuid.uuid4().hex}"
            assignments[key] = {
                "source_worker_id": source_worker_id,
                "source_name": source.get("name") or source_worker_id,
                "source_address": source.get("address") or source.get("hostname") or "",
                "target_worker_id": target_worker_id,
                "target_name": target.get("name") or target_worker_id,
                "target_address": target.get("address")
                or target.get("hostname")
                or "",
                "devices": requested_devices,
                "status": "connecting",
                "generation": generation,
                "operation_id": operation_id,
                "updated_at": time.time(),
            }
            self.save_assignments(assignments)

        source_started = False
        try:
            await _run_worker_command(
                source_worker_id,
                "adb_proxy",
                {
                    "action": "source_start",
                    "source_worker_id": source_worker_id,
                    "listen_address": source_address,
                    "allowed_peer_address": target_address,
                    "devices": requested_devices,
                    "access_token": grant,
                    "generation": generation,
                    "operation_id": operation_id,
                },
                timeout=20,
            )
            source_started = True
            result = await _run_worker_command(
                target_worker_id,
                "adb_proxy",
                {
                    "action": "target_connect",
                    "source_worker_id": source_worker_id,
                    "source_address": source_address,
                    "devices": requested_devices,
                    "access_token": grant,
                    "generation": generation,
                    "operation_id": operation_id,
                },
                timeout=90,
            )
        except Exception:
            restored = False
            if previous_assignment:
                try:
                    restore_grant = create_pair_grant(
                        source_worker_id,
                        target_worker_id,
                        local_worker_id,
                    )
                    await _run_worker_command(
                        source_worker_id,
                        "adb_proxy",
                        {
                            "action": "source_start",
                            "source_worker_id": source_worker_id,
                            "listen_address": source_address,
                            "allowed_peer_address": target_address,
                            "devices": previous_devices,
                            "access_token": restore_grant,
                            "generation": int(previous_assignment.get("generation") or 0),
                            "operation_id": f"adb-proxy-restore-{uuid.uuid4().hex}",
                        },
                        timeout=20,
                    )
                    await _run_worker_command(
                        target_worker_id,
                        "adb_proxy",
                        {
                            "action": "target_connect",
                            "source_worker_id": source_worker_id,
                            "source_address": source_address,
                            "devices": previous_devices,
                            "access_token": restore_grant,
                            "generation": int(previous_assignment.get("generation") or 0),
                            "operation_id": f"adb-proxy-restore-{uuid.uuid4().hex}",
                        },
                        timeout=90,
                    )
                    restored = True
                except Exception:
                    pass
            elif source_started:
                try:
                    await _run_worker_command(
                        source_worker_id,
                        "adb_proxy",
                        {
                            "action": "source_stop",
                            "generation": generation,
                            "operation_id": operation_id,
                        },
                        timeout=15,
                    )
                except Exception:
                    pass
            with self._lock:
                assignments = self.assignments()
                if previous_assignment:
                    previous_assignment.update({
                        "status": "connected" if restored else "connect_failed",
                        "updated_at": time.time(),
                    })
                    assignments[key] = previous_assignment
                else:
                    assignments.pop(key, None)
                self.save_assignments(assignments)
            raise

        with self._lock:
            assignments = self.assignments()
            assignment = assignments.get(key) or {}
            assignment.update({
                "status": "connected",
                "updated_at": time.time(),
            })
            assignments[key] = assignment
            self.save_assignments(assignments)
        return {
            "success": True,
            "message": (
                f"ADB设备已从 {source_worker_id} 接入 {target_worker_id}；"
                f"设备：{', '.join(requested_devices)}；"
                "目标主机可执行ADB/测试操作，Fastboot、锁定和烧写操作已禁用"
            ),
            "assignment": assignment,
            **result,
        }

    async def disconnect(
        self,
        source_worker_id: str,
        target_worker_id: str,
    ) -> dict[str, Any]:
        async with self._operation_lock:
            return await self._disconnect(source_worker_id, target_worker_id)

    async def _disconnect(
        self,
        source_worker_id: str,
        target_worker_id: str,
    ) -> dict[str, Any]:
        from features.cluster.api import _run_worker_command

        key = self._assignment_key(source_worker_id, target_worker_id)
        assignment = self.assignments().get(key)
        if not assignment:
            return {
                "success": True,
                "message": "ADB Proxy接入已经断开",
                "already_disconnected": True,
            }
        # disconnect does NOT restart the target's ADB daemon, so the running
        # test check from _require_idle_target is unnecessary.  But disconnect
        # MUST be blocked when the *proxied* devices are actively claimed —
        # removing the proxy route mid-operation would break the test.  Only
        # check the devices that belong to this assignment, not every device
        # on the target host (which may include unrelated local-USB devices).
        proxy_devices = {
            str(serial or "").strip()
            for serial in assignment.get("devices") or []
            if str(serial or "").strip()
        }
        self._require_proxy_devices_not_claimed(target_worker_id, proxy_devices)
        target_error = None
        disconnect_generation = max(
            int(time.time() * 1000),
            int(assignment.get("generation") or 0) + 1,
        )
        operation_id = f"adb-proxy-disconnect-{uuid.uuid4().hex}"
        try:
            await _run_worker_command(
                target_worker_id,
                "adb_proxy",
                {
                    "action": "target_disconnect",
                    "source_worker_id": source_worker_id,
                    "generation": disconnect_generation,
                    "operation_id": operation_id,
                },
                timeout=90,
            )
        except Exception as exc:
            target_error = exc
        source_error = None
        try:
            await _run_worker_command(
                source_worker_id,
                "adb_proxy",
                {
                    "action": "source_stop",
                    "generation": disconnect_generation,
                    "operation_id": operation_id,
                },
                timeout=15,
            )
        except Exception as exc:
            source_error = exc
        if target_error is not None or source_error is not None:
            with self._lock:
                assignments = self.assignments()
                current = assignments.get(key) or assignment
                current["status"] = "disconnect_failed"
                current["updated_at"] = time.time()
                assignments[key] = current
                self.save_assignments(assignments)
            raise target_error or source_error
        with self._lock:
            assignments = self.assignments()
            assignments.pop(key, None)
            self.save_assignments(assignments)
        return {
            "success": True,
            "message": f"已断开 {source_worker_id} → {target_worker_id} 的ADB接入",
            "source_worker_id": source_worker_id,
            "target_worker_id": target_worker_id,
        }

    @staticmethod
    def _assignment_key(source_worker_id: str, target_worker_id: str) -> str:
        return f"{source_worker_id}|{target_worker_id}"

    @staticmethod
    def _online_proxy_worker(worker_id: str) -> dict[str, Any]:
        from features.cluster import get_cluster_service

        cluster = get_cluster_service()
        worker = cluster.repository.get_worker(worker_id)
        if not worker:
            raise HTTPException(404, f"Worker不存在: {worker_id}")
        if worker.get("status") not in {"online", "busy"}:
            raise HTTPException(409, f"Worker当前离线: {worker_id}")
        capabilities = worker.get("capabilities") or {}
        installed = bool(capabilities.get("adb_proxy"))
        version = str(capabilities.get("adb_proxy_version") or "")
        if worker_id == cluster.config.local_worker_id:
            from worker_agent.adb_proxy import capability_status

            local_capability = capability_status()
            installed = bool(local_capability.get("installed"))
            version = str(local_capability.get("version") or "")
        if not installed:
            raise HTTPException(
                409, f"{worker_id} 尚未安装adbproxy-rs，请重新部署Worker"
            )
        if not ADBProxyService._compatible_proxy_version(version):
            raise HTTPException(
                409,
                f"{worker_id} 的adbproxy-rs版本不兼容，"
                f"请升级到 {ADB_PROXY_VERSION}",
            )
        if not (worker.get("address") or worker.get("hostname")):
            raise HTTPException(409, f"{worker_id} 没有可连接地址")
        return worker

    @staticmethod
    def _compatible_proxy_version(version: str) -> bool:
        import re

        return re.search(
            rf"(?<!\d){re.escape(ADB_PROXY_VERSION)}(?!\d)",
            str(version or ""),
        ) is not None

    @staticmethod
    def _require_idle_target(worker_id: str, worker: dict[str, Any]) -> None:
        from features.cluster import get_cluster_service

        if worker.get("status") != "online" or int(
            worker.get("running_jobs") or 0
        ) > 0:
            raise HTTPException(
                409,
                f"{worker_id} 正在执行测试，不能重启ADB服务",
            )
        claimed = [
            str(item.get("serial") or "")
            for item in get_cluster_service().repository.list_devices(worker_id)
            if item.get("state") in {"allocated", "reserved", "external_busy"}
        ]
        if claimed:
            raise HTTPException(
                409,
                f"{worker_id} 存在占用中的设备，不能重启ADB服务: "
                + ", ".join(claimed),
            )

    @staticmethod
    def _require_proxy_devices_not_claimed(
        worker_id: str, proxy_devices: set[str]
    ) -> None:
        """Block disconnect only when the proxied devices are actively claimed.

        Unlike _require_idle_target this does NOT check running_jobs because
        disconnect does not restart the target ADB daemon.  It only guards
        against removing a proxy route while a proxied device is in use.
        Devices on the target that are NOT part of this proxy assignment
        (e.g. local-USB devices) are ignored.
        """
        if not proxy_devices:
            return
        from features.cluster import get_cluster_service

        try:
            claimed = [
                str(item.get("serial") or "")
                for item in get_cluster_service().repository.list_devices(worker_id)
                if item.get("state") in {"allocated", "reserved", "external_busy"}
                and str(item.get("serial") or "") in proxy_devices
            ]
        except (AttributeError, RuntimeError, TypeError):
            return
        if claimed:
            raise HTTPException(
                409,
                f"{worker_id} 存在被占用的代理设备，不能断开ADB接入: "
                + ", ".join(claimed),
            )


adb_proxy_service = ADBProxyService()
