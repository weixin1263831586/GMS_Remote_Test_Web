"""Durable AOA dispatch and post-flash verification for ATS runs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from foundation.aoa_calibration import mapping_options
from foundation.aoa_hid_mode import profile_hid_options
from foundation.aoa_profile import MAX_AOA_OPERATION_SECONDS
from foundation.aoa_recovery_policy import (
    firmware_compatibility,
    require_verified_recovery,
    target_firmware_context,
)
from foundation.error_model import ApiError


class RecoveryExecutorMixin:
    def snapshot_devices(self, repository, worker_id: str, devices: list[dict], run: dict) -> list[dict]:
        inventory = {item["id"]: item for item in repository.list_devices(worker_id)}
        snapshots = [{"serial": item["id"], "usb_port_path": str(
            (inventory.get(item["id"], {}).get("properties") or {}).get("usb") or "")}
            for item in devices]
        if self.aoa_enabled(run) and any(not d["usb_port_path"] for d in snapshots):
            raise ValueError("AOA recovery requires a physical USB port before flashing")
        if self.aoa_enabled(run):
            from features.devices import replay_mapping_options

            from .executors import _run_test_plan

            recovery = _run_test_plan(run)["aoa"]
            commands = recovery.get("commands") or []
            metadata = recovery.get("metadata") or {}
            mode = profile_hid_options(metadata)["aoa_hid_touch_mode"]
            owner = str(run.get("created_by") or run.get("owner") or "")
            for snapshot in snapshots:
                properties = inventory.get(snapshot["serial"], {}).get("properties") or {}
                context = target_firmware_context(metadata, commands, properties.get("aoa_context") or {})
                mappings = replay_mapping_options(metadata, context, owner, mode, commands=commands)
                require_verified_recovery(commands, mappings, metadata)
                snapshot["aoa_mapping_options"] = mappings
                snapshot["aoa_firmware_compatibility"] = firmware_compatibility(metadata, commands)
        return snapshots

    @staticmethod
    def recovery_mapping_options(recovery: dict, identity: dict) -> dict:
        metadata = recovery.get("metadata") or {}
        commands = recovery.get("commands") or []
        frozen = identity.get("aoa_mapping_options") or {}
        calibration = metadata.get("calibration") or {}
        if calibration.get("mapping") and not frozen.get("aoa_source_touch_mapping") and not frozen.get("aoa_semantic_plan"):
            raise ValueError("AOA mapping snapshot is missing; create a new run before flashing")
        mappings = mapping_options({**profile_hid_options(metadata), **frozen})
        require_verified_recovery(commands, mappings, metadata)
        if identity.get("aoa_firmware_compatibility") != firmware_compatibility(metadata, commands):
            raise ValueError("AOA firmware compatibility snapshot changed; create a new run")
        return mappings

    def recovery_preflash_error(self, cluster, worker_id: str, run: dict) -> str:
        if not self.aoa_enabled(run):
            return ""
        devices = json.loads(run.get("devices_json") or "[]")
        if not devices or any(not item.get("usb_port_path") for item in devices):
            return "AOA recovery requires a frozen physical USB identity before flashing"
        worker = cluster.repository.get_worker(worker_id) or {}
        capabilities = worker.get("capabilities") or {}
        if capabilities.get("aoa_identity_fencing_v1") is not True:
            return "Worker AOA safety contract is outdated; update the Worker before flashing"
        if capabilities.get("aoa_hid_modes_v1") is not True:
            return "Worker does not support explicit AOA HID modes; update it before flashing"
        from .executors import _run_test_plan

        recovery = _run_test_plan(run).get("aoa") or {}
        options = profile_hid_options(recovery.get("metadata"))
        try:
            mappings = [self.recovery_mapping_options(recovery, item) for item in devices]
            from features.devices import validate_owned_semantic

            for mapping in mappings:
                validate_owned_semantic(mapping, str(run.get("created_by") or run.get("owner") or ""))
            from foundation.aoa_semantics import require_capture_capability

            for mapping, identity in zip(mappings, devices):
                if mapping.get("aoa_semantic_plan"):
                    require_capture_capability(mapping, capabilities, identity["serial"], identity["usb_port_path"])
        except (ValueError, ApiError) as exc:
            return str(exc)
        if any(item.get("aoa_semantic_plan") for item in mappings) and capabilities.get("aoa_semantic_v1") is not True:
            return "Worker does not support certified semantic recovery; update it before flashing"
        if (any(item.get("aoa_touch_mapping") or item.get("aoa_source_touch_mapping") for item in mappings)
                and capabilities.get("aoa_affine_mapping_v2") is not True):
            return "Worker AOA mapping protocol is outdated; update it before flashing"
        if options["aoa_hid_session_mode"] == "per_command" and capabilities.get("aoa_per_command_v1") is not True:
            return "Worker does not support per-command AOA playback; update it before flashing"
        if (capabilities.get("aoa_usb_hid_v1") is not True
                or capabilities.get("aoa_record_input_v1") is not True):
            return "Worker AOA recovery requires pyusb and ADB before flashing"
        return ""

    def aoa_enabled(self, run: dict[str, Any]) -> bool:
        """AOA 恢复阶段开关：plan.aoa.mode == user_build_recovery 时启用。"""
        try:
            from .executors import _run_test_plan

            plan = _run_test_plan(run)
        except Exception:
            return False
        recovery = plan.get("aoa") if isinstance(plan.get("aoa"), dict) else {}
        return recovery.get("mode") == "user_build_recovery"

    def aoa_recover(self, run: dict[str, Any]) -> dict[str, Any]:
        """AOA 恢复阶段（ADR 0022）：user build 烧写后模拟 UI 打开 USB 调试。

        通过既有 device_action 通道派发 ``aoa_sequence``，payload 携带
        ``automation_run_id``：远程 Worker 路径由 Controller 按
        ``automation:{run_id}:aoa_sequence`` 派生 operation_id 做数据库层
        去重，派发成功但响应丢失后的重试收敛到同一条命令，不会重放
        USB 调试开关点击（run 侧仍以 ``aoa_command_id`` 持久化为准）。
        命令序列来自运行创建时冻结的 ``aoa.commands`` 快照，
        语法与摘要在 Controller 侧复核。
        """
        from .executors import _run_test_plan

        state = json.loads(run.get("result_json") or "{}")
        if state.get("stage_started_at"):
            started = datetime.fromisoformat(state["stage_started_at"].replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - started).total_seconds() > MAX_AOA_OPERATION_SECONDS:
                return {"success": False, "reconcile_required": True,
                        "error": "AOA recovery budget exceeded; inspect the operation before retrying"}
        plan = _run_test_plan(run)
        recovery = plan.get("aoa") if isinstance(plan.get("aoa"), dict) else {}
        if not recovery or recovery.get("mode") in {None, "", "off"}:
            return {"success": True, "skipped": True}
        from .executors import _run_devices

        devices = _run_devices(run)
        if len(devices) != 1:
            return {"success": False, "error": "AOA recovery requires exactly one device"}
        worker_id = str(run.get("worker_id") or "")
        identity = json.loads(run.get("devices_json") or "[]")[0]
        if not identity.get("usb_port_path"):
            return {"success": False, "error": "AOA run has no frozen physical USB identity"}
        from .aoa_snapshot import commands_digest

        commands = [str(item) for item in recovery.get("commands") or []]
        if recovery.get("commands_sha256") != commands_digest(commands):
            return {"success": False, "error": "AOA snapshot missing or changed; create a new run"}
        if not commands:
            return {"success": False, "error": "AOA recovery has no commands configured"}
        try:
            from foundation import aoa_profile

            aoa_profile.parse_profile_commands(commands)
            mappings = self.recovery_mapping_options(recovery, identity)
            from features.devices import validate_owned_semantic

            validate_owned_semantic(mappings, str(run.get("created_by") or run.get("owner") or ""))
        except (ValueError, ApiError) as exc:
            return {"success": False, "error": str(exc)}
        wait_adb_seconds = int(recovery.get("wait_adb_seconds") or 0)

        command_id = str(run.get("aoa_command_id") or "")
        if command_id:
            try:
                polled = self._json_response(self.session.get(
                    self._url(f"/api/cluster/commands/{command_id}"),
                    headers=self._auth_headers(run), timeout=30
                ))
            except Exception as exc:
                return {"success": True, "running": True, "command_id": command_id,
                        "poll_error": str(exc)}
            command = polled.get("response", {}).get("command") or polled.get("command") or {}
            status = str(command.get("status") or "")
            if status in {"queued", "delivered", "accepted", "running"}:
                stamp = command.get("created_at")
                if stamp:
                    created = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - created).total_seconds() > MAX_AOA_OPERATION_SECONDS:
                        return {"success": False, "reconcile_required": True,
                                "error": "AOA operation exceeded its budget; inspect device before retrying",
                                "command_id": command_id}
                return {"success": True, "running": True, "command_id": command_id,
                        "command_status": status}
            if status != "completed":
                return {"success": False, "reconcile_required": True, "command_id": command_id,
                        "error": command.get("error") or f"AOA recovery {status or 'unknown'}"}
            aoa_result = command.get("result")
            if not isinstance(aoa_result, dict):
                aoa_result = {}
            if aoa_result.get("success") is False:
                return {
                    "success": False,
                    "command_id": command_id,
                    "error": aoa_result.get("error") or "AOA recovery result failed",
                    "aoa_result": aoa_result,
                    "reconcile_required": bool(aoa_result.get("reconcile_required")),
                }
            return {"success": True, "running": False, "command_id": command_id,
                    "aoa_result": aoa_result}

        response = None
        try:
            # /api/cluster/devices/actions 是 ClusterDeviceAction JSON 端点：
            # machine principal（ADR 0012）只能操作本 run 预约内的设备，
            # 设备租约由端点内的 claim 流程保证。
            response = self.session.post(
                self._url("/api/cluster/devices/actions"),
                json={
                    "worker_id": worker_id,
                    "devices": list(devices),
                    "action": "aoa_sequence",
                    "aoa_commands": commands,
                    **profile_hid_options(recovery.get("metadata")),
                    **mappings,
                    "wait_adb_seconds": max(wait_adb_seconds, 1),
                    "usb_port_path": str((json.loads(run.get("devices_json") or "[]")[0]).get("usb_port_path") or ""),
                    # 幂等重放键：远程路径按 run+action 派生确定性
                    # operation_id，响应丢失后的重试不再整段重放点击序列。
                    "automation_run_id": str(run.get("id") or ""),
                },
                headers=self._auth_headers(run),
                timeout=30,
            )
            result = self._json_response(response)
        except Exception as exc:
            response_status = getattr(getattr(exc, "response", None), "status_code", 0) or getattr(response, "status_code", 0)
            if 400 <= response_status < 500:
                return {"success": False, "reconcile_required": True,
                        "error": f"AOA recovery request rejected (HTTP {response_status}): {exc}"}
            # The Controller may already have persisted and started the operation.
            # Follow the same operation key on the next tick; retain the reservation.
            return {"success": True, "running": True, "dispatch_error": str(exc)}
        command_id = str(
            result.get("response", {}).get("command_id") or result.get("command_id") or "")
        if not result.get("success"):
            return result
        if not command_id:
            return {"success": False, "reconcile_required": True, "error": "AOA recovery did not return a durable command id"}
        return {"success": True, "running": True, "command_id": command_id,
                "deduplicated": bool(result.get("response", {}).get("deduplicated"))}

    def verify_post_flash(self, run: dict[str, Any]) -> dict[str, Any]:
        from .executors import _run_devices, _run_test_plan

        flash = _run_test_plan(run).get("flash") or {}
        if flash.get("mode") == "skip":
            return {"success": True, "verified": True, "skipped": True}
        verify = flash.get("verify") or {}
        checked = self._verify_cluster_post_flash(
            run, str(run.get("worker_id") or ""), _run_devices(run), verify)
        if checked.get("success") or not checked.get("retry"):
            return checked
        budget = max(60, int(verify.get("retries") or 30) * int(verify.get("retry_delay") or 10))
        state = json.loads(run.get("result_json") or "{}")
        started = datetime.fromisoformat(str(state.get("stage_started_at") or run["updated_at"]).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - started).total_seconds() < budget:
            return {**checked, "success": True, "running": True}
        return checked
