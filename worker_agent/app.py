from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from foundation.network_quality import probe_tcp_quality
from foundation.transport_contract import TransportOperationError

from .adb_proxy import (
    capability_status as adb_proxy_capability_status,
)
from .adb_proxy import (
    execute_adb_proxy_action,
    pair_code_from_grant,
)
from .adb_proxy import (
    recover_managed_state as recover_adb_proxy_state,
)
from .android_inspection import _aapt2_path, prepare_device_export
from .client import ControllerClient
from .command_events import CommandEventUploader
from .config import WorkerConfig
from .inventory import (
    browse_directory,
    copy_image_into,
    execute_device_action,
    execute_suite_action,
    execute_usbip_action,
    flash_firmware,
    flash_gsi,
    host_metrics,
    import_suite_report,
    prepare_suite_export,
    probe_devices,
    scan_suites,
)
from .process_inventory import (
    discover_tradefed_processes,
    process_inventory_capability_status,
)
from .runtime import WorkerRuntime


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gms-worker")
AGENT_VERSION = "0.5.1"


VNC_PORT = 5900
NOVNC_PORT = 6080


def _rfb_handshake_ok(port: int = VNC_PORT, timeout: float = 1.0) -> bool:
    """Return True only when a real RFB greeting is received, not just an open port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(12).startswith(b"RFB ")
    except OSError:
        return False


def restart_local_vnc() -> dict:
    """Kill stale x11vnc/websockify and restart them. Runs on the worker host itself."""
    units = (
        "gms-worker-xvfb.service",
        "gms-worker-x11vnc.service",
        "gms-worker-novnc.service",
    )
    unit_root = Path.home() / ".config/systemd/user"
    if all((unit_root / unit).is_file() for unit in units):
        completed = subprocess.run(
            ["systemctl", "--user", "restart", *units],
            capture_output=True,
            text=True,
            timeout=20,
        )
        time.sleep(1)
        rfb_ok = _rfb_handshake_ok()
        return {
            "x11vnc_running": rfb_ok,
            "websockify_listening": _port_listening(NOVNC_PORT),
            "rfb_ok": rfb_ok,
            "systemd_exit_code": completed.returncode,
            "error": completed.stderr.strip(),
        }

    return {
        "x11vnc_running": False,
        "websockify_listening": False,
        "rfb_ok": False,
        "error": (
            "managed noVNC systemd units are missing; redeploy this Worker "
            "to install the private, token-protected services"
        ),
    }


def stop_local_worker_agent() -> None:
    """Stop this host's managed Worker services after Controller ACK."""
    units = [
        "gms-worker-agent.service",
        "gms-worker-xvfb.service",
        "gms-worker-x11vnc.service",
        "gms-worker-novnc.service",
    ]
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", *units],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _port_listening(port: int) -> bool:
    result = subprocess.run(["ss", "-ltn"], capture_output=True, text=True)
    return f":{port} " in result.stdout


def _safe_adb_proxy_status() -> dict:
    try:
        return execute_adb_proxy_action("status")
    except Exception as exc:
        logger.warning("ADB Proxy status probe failed: %s", exc)
        return {
            "transport_state": "failed",
            "protocol_state": "unknown",
            "readiness": "not_ready",
            "error": str(exc),
        }


class WorkerAgent:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.client = ControllerClient(config)
        self.runtime = WorkerRuntime(config)
        self.runtime.prune_inactive_fences()
        self.session_id = f"worker-session-{uuid.uuid4().hex}"
        self.suites = []
        self.last_suite_scan = 0.0
        self.next_usbip_recovery_at = 0.0
        self.usbip_operation_lock = threading.Lock()

    def registration(self):
        try:
            _aapt2_path()
            has_aapt2 = True
        except RuntimeError:
            has_aapt2 = False
        adb_proxy = adb_proxy_capability_status()
        process_inventory = process_inventory_capability_status()
        capabilities = {"adb": shutil.which("adb") is not None,
                        "fastboot": shutil.which("fastboot") is not None,
                        "tradefed": True,
                        "cts": True, "gts": True, "vts": True, "sts": True,
                        "device_inspection": True,
                        "usbip_client": (
                            shutil.which("usbip") is not None
                            and os.access("/usr/local/libexec/gms-worker-usbip", os.X_OK)
                        ),
                        "usbip_preflight": True,
                        "aapt2": has_aapt2,
                        "adb_proxy": bool(adb_proxy.get("installed")),
                        "adb_proxy_version": str(adb_proxy.get("version") or ""),
                        "adb_proxy_logs": True,
                        "process_inventory_backend": process_inventory["backend"],
                        "process_inventory_contract_version": process_inventory[
                            "contract_version"
                        ],
                        "adb_proxy_source_only": self.config.source_only,
                        "ssh_user": self.config.ssh_user or getpass.getuser()}
        if self.config.source_only:
            capabilities.update({
                "fastboot": False,
                "tradefed": False,
                "cts": False,
                "gts": False,
                "vts": False,
                "sts": False,
                "device_inspection": False,
                "usbip_client": False,
                "usbip_preflight": False,
                "aapt2": False,
            })
        # noVNC 能力要求端口开放且 RFB 握手有效。
        if _port_listening(NOVNC_PORT) and _rfb_handshake_ok():
            capabilities["novnc_port"] = NOVNC_PORT
        return {"worker_id": self.config.worker_id, "name": self.config.name,
                "hostname": socket.gethostname(),
                "address": self.config.address or socket.gethostname(),
                "agent_version": AGENT_VERSION, "max_jobs": self.config.max_jobs,
                "session_id": self.session_id,
                "capabilities": capabilities}

    def heartbeat(self):
        proxy_recovery = recover_adb_proxy_state(secret=self.config.token)
        if proxy_recovery["recovered"]:
            logger.info(
                "recovered ADB Proxy roles: %s",
                ", ".join(proxy_recovery["recovered"]),
            )
        if proxy_recovery["errors"]:
            logger.debug(
                "ADB Proxy recovery pending: %s",
                "; ".join(proxy_recovery["errors"]),
            )
        if time.monotonic() >= self.next_usbip_recovery_at:
            usbip_recovery = self.recover_usbip_assignments()
            if usbip_recovery["recovered"]:
                logger.info(
                    "recovered USB/IP assignments: %s",
                    ", ".join(usbip_recovery["recovered"]),
                )
            if usbip_recovery["errors"]:
                logger.warning(
                    "USB/IP recovery pending: %s",
                    "; ".join(usbip_recovery["errors"]),
                )
                self.next_usbip_recovery_at = time.monotonic() + 30
            else:
                self.next_usbip_recovery_at = (
                    time.monotonic() + 60
                    if self.runtime.usbip_assignments()
                    else float("inf")
                )
        now = time.monotonic()
        include_suites = not self.suites or now - self.last_suite_scan >= self.config.suite_scan_interval
        if include_suites:
            self.suites = scan_suites(self.config)
            self.last_suite_scan = now
        managed_jobs = self.runtime.running_jobs()
        running_jobs = managed_jobs + discover_tradefed_processes(managed_jobs)
        command_states = self.runtime.unsynced_commands()
        payload = {"agent_version": AGENT_VERSION, **host_metrics(self.config),
                   "running_jobs": running_jobs, "devices": probe_devices(include_details=True),
                   "adb_proxy": _safe_adb_proxy_status(),
                   "session_id": self.client.session_id or self.session_id,
                   "connection_generation": self.client.connection_generation,
                   "command_states": command_states,
                   "timestamp": datetime.now(timezone.utc).isoformat()}
        if include_suites:
            payload["suites"] = self.suites
        response = self.client.heartbeat(payload)
        for attempt_id in response.get("revoked_attempt_ids", []):
            self.runtime.revoke_attempt(
                str(attempt_id),
                "Controller revoked the expired or superseded device claim",
            )
        self.runtime.mark_commands_synced(
            [str(item) for item in response.get("reconciled_command_ids", [])]
        )

    def _ack_command(
        self, command_id: str, status: str, result: dict | None = None, error: str = ""
    ):
        response = (
            self.client.ack(command_id, status, error=error)
            if result is None
            else self.client.ack(command_id, status, result, error)
        )
        self.runtime.mark_command_synced(command_id)
        return response

    def handle(self, command):
        previous = self.runtime.previous_command(command["id"])
        if previous:
            # Controller 可能重复投递，运行中的命令不得再次执行。
            if command.get("command_type") == "device_action":
                self.runtime.release_fencing(command)
            self._ack_command(command["id"], previous["status"], previous["result"], previous["error"])
            return
        release_after_command = command.get("command_type") == "device_action"
        try:
            self.runtime.validate_fencing(command)
            kind = command["command_type"]
            if kind == "refresh_devices":
                result = {"devices": probe_devices(include_details=True)}
            elif kind == "refresh_suites":
                self.suites = scan_suites(self.config)
                self.last_suite_scan = time.monotonic()
                result = {"suites": self.suites}
            elif kind == "device_action":
                payload = command.get("payload", {})
                result = execute_device_action(payload.get("action", ""), payload.get("devices", []), payload)
            elif kind == "adb_proxy":
                payload = command.get("payload", {})
                action = str(payload.get("action") or "")
                pair_code = ""
                if action == "source_start":
                    pair_code = pair_code_from_grant(
                        self.config.token,
                        str(payload.get("access_token") or ""),
                    )
                elif action == "target_connect":
                    pair_code = self.client.adb_proxy_pair_code(
                        str(payload.get("source_worker_id") or ""),
                        str(payload.get("access_token") or ""),
                    )
                result = execute_adb_proxy_action(
                    action,
                    payload,
                    pair_code=pair_code,
                )
                if action in {"target_connect", "target_disconnect"}:
                    try:
                        # Publish the changed adb-hub inventory before the
                        # command ACK, so the Controller/UI can refresh at once.
                        self.heartbeat()
                    except Exception:
                        logger.warning(
                            "failed to publish ADB Proxy inventory immediately",
                            exc_info=True,
                        )
            elif kind in {"usbip_attach", "usbip_detach"}:
                self.runtime.save_command(command["id"], "running", {})
                self._ack_command(command["id"], "running", {})
                threading.Thread(
                    target=self.run_usbip_action,
                    args=(command,),
                    name=f"USBIP-{command['id']}",
                    daemon=True,
                ).start()
                return
            elif kind == "usbip_preflight":
                payload = command.get("payload", {})
                source_host = str(payload.get("source_host") or "").strip("[]")
                result = {
                    "network_quality": probe_tcp_quality(source_host, 3240)
                }
            elif kind == "suite_action":
                if command.get("payload", {}).get("action") in {"download_url", "extract"}:
                    self.runtime.save_command(command["id"], "running", {})
                    self._ack_command(command["id"], "running", {})
                    threading.Thread(target=self.run_suite_action,
                                     args=(command,), name=f"SuiteAction-{command['id']}", daemon=True).start()
                    return
                result = execute_suite_action(self.config, command.get("payload", {}))
            elif kind == "suite_export":
                self.runtime.save_command(command["id"], "running", {})
                self._ack_command(command["id"], "running", {})
                threading.Thread(target=self.run_suite_export,
                                 args=(command,), name=f"SuiteExport-{command['id']}", daemon=True).start()
                return
            elif kind == "file_transfer":
                self.runtime.save_command(command["id"], "running", {})
                self._ack_command(command["id"], "running", {})
                threading.Thread(target=self.run_file_transfer,
                                 args=(command,), name=f"FileTransfer-{command['id']}", daemon=True).start()
                return
            elif kind == "file_browse":
                payload = command.get("payload") or {}
                # 默认打开套件根目录（如 ~/GMS-Suite），未配置则回落主目录。
                default_dir = next(
                    (root for root in self.config.suite_roots
                     if Path(root).expanduser().exists()),
                    None,
                )
                result = browse_directory(
                    str(payload.get("path") or ""), default_path=default_dir
                )
            elif kind == "report_import":
                self.runtime.save_command(command["id"], "running", {})
                self._ack_command(command["id"], "running", {})
                threading.Thread(target=self.run_report_import,
                                 args=(command,), name=f"ReportImport-{command['id']}", daemon=True).start()
                return
            elif kind == "device_export":
                self.runtime.save_command(command["id"], "running", {})
                self._ack_command(command["id"], "running", {})
                threading.Thread(target=self.run_device_export,
                                 args=(command,), name=f"DeviceExport-{command['id']}", daemon=True).start()
                return
            elif kind in {"flash_firmware", "flash_gsi"}:
                self.runtime.save_command(command["id"], "running", {})
                self._ack_command(command["id"], "running", {})
                threading.Thread(target=self.run_firmware_flash, args=(command,),
                                 name=f"Firmware-{command['id']}", daemon=True).start()
                return
            elif kind == "start_test":
                result = self.runtime.start_process(command)
                self.runtime.save_command(command["id"], "running", result)
                self._ack_command(command["id"], "running", result)
                threading.Thread(
                    target=self.monitor_job,
                    args=(command["id"], result["worker_job_id"]),
                    name=f"JobMonitor-{result['worker_job_id']}", daemon=True,
                ).start()
                return
            elif kind == "stop_test":
                result = self.runtime.stop_process(command.get("payload", {}).get("worker_job_id", ""))
            elif kind == "restart_vnc":
                result = restart_local_vnc()
            elif kind == "check_vpn":
                try:
                    proc = subprocess.run(
                        ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'connection', 'show', '--active'],
                        capture_output=True, text=True, timeout=3,
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(
                            (proc.stderr or proc.stdout or "nmcli status check failed").strip()
                        )
                    connected = False
                    for line in proc.stdout.splitlines():
                        parts = line.split(':')
                        if len(parts) >= 2 and parts[1].strip().lower() == 'vpn':
                            connected = True
                            break
                    result = {"connected": connected}
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RuntimeError("nmcli status check failed") from exc
            elif kind == "list_vpn_connections":
                # 列出本机 NetworkManager 中预配置的全部 VPN 连接（含未激活），
                # 供控制台"连接VPN"弹框按所选 Worker 主机展示账号。
                try:
                    proc = subprocess.run(
                        ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
                        capture_output=True, text=True, timeout=5,
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(
                            (proc.stderr or proc.stdout or "nmcli connection listing failed").strip()
                        )
                    connections = [
                        line.split(':', 1)[0].strip()
                        for line in proc.stdout.splitlines()
                        if len(line.split(':', 1)) == 2
                        and line.split(':', 1)[1].strip().lower() == 'vpn'
                        and line.split(':', 1)[0].strip()
                    ]
                    result = {"connections": connections}
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RuntimeError("nmcli connection listing failed") from exc
            elif kind == "connect_vpn":
                # 在本机 NetworkManager 上激活指定 VPN 连接（凭据由
                # NetworkManager 保存，无需回传密码）。
                vpn_name = str((command.get("payload") or {}).get("vpn_name") or "").strip()
                if not vpn_name:
                    raise ValueError("vpn_name is required")
                try:
                    proc = subprocess.run(
                        ['nmcli', 'connection', 'up', vpn_name],
                        capture_output=True, text=True, timeout=60,
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(
                            (proc.stderr or proc.stdout or "nmcli connect failed").strip()
                        )
                    result = {"connected": True, "vpn_connection_name": vpn_name}
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f"nmcli connect timed out: {vpn_name}") from exc
                except OSError as exc:
                    raise RuntimeError("nmcli connect failed") from exc
            elif kind == "uninstall_agent":
                # 先确认回执，再停止服务，确保 Controller 能移除注册记录。
                result = {"stopping": True, "removed_data": False}
                self.runtime.save_command(command["id"], "completed", result)
                self._ack_command(command["id"], "completed", result)
                threading.Thread(
                    target=stop_local_worker_agent,
                    name="StopWorkerAgent",
                    daemon=True,
                ).start()
                return
            elif kind == "get_config":
                result = self.read_worker_config()
            elif kind == "update_config":
                result = self.update_worker_config(command.get("payload", {}))
            else:
                raise ValueError(f"unsupported command type: {kind}")
            self.runtime.save_command(command["id"], "completed", result)
            if release_after_command:
                self.runtime.release_fencing(command)
            self._ack_command(command["id"], "completed", result)
        except Exception as exc:
            logger.exception("command %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            if release_after_command:
                self.runtime.release_fencing(command)
            self._ack_command(command["id"], "failed", error=str(exc))

    # ---- 可配置参数读写（通过 Controller 远程下发） ----

    _CONFIG_FIELDS = {"max_jobs": int}

    def _config_path(self) -> Path:
        return Path(os.getenv("GMS_WORKER_CONFIG",
                              Path.home() / ".config/gms-worker/config.json"))

    def read_worker_config(self) -> dict:
        """Return the configurable fields exposed to the cluster UI."""
        import json
        path = self._config_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return {key: raw.get(key) for key in self._CONFIG_FIELDS}

    def update_worker_config(self, updates: dict) -> dict:
        """Persist and apply whitelisted fields without restarting the Agent."""
        import json
        path = self._config_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        changed = {}
        for key, caster in self._CONFIG_FIELDS.items():
            if key in updates:
                try:
                    raw[key] = caster(updates[key])
                    changed[key] = raw[key]
                except (TypeError, ValueError):
                    raise ValueError(f"invalid value for {key}") from None
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            if "max_jobs" in changed:
                self.config.max_jobs = changed["max_jobs"]
            logger.info("worker config updated and applied: %s", changed)
        return {"updated": changed, "applied": bool(changed), "restarted": False}

    def monitor_job(self, command_id: str, worker_job_id: str):
        row = None
        try:
            # Resolve controller identifiers from the durable jobs table.
            with self.runtime.connect() as conn:
                row = conn.execute(
                    """SELECT job_id,attempt_id,work_dir,trace_id,operation_id
                       FROM jobs WHERE worker_job_id=?""",
                                   (worker_job_id,)).fetchone()
            if row:
                sequence = 0
                # 任务开始时上报设备指纹（"测的包 ≠ 刷的包"一眼识别），
                # 每条指纹占用一个 sequence，日志从其后继续编号。
                sequence += self._report_device_fingerprints(row)
                offsets = {"stdout.log": 0, "stderr.log": 0}
                while self.runtime.process_poll(worker_job_id) is None:
                    try:
                        sequence = self._flush_log_events(row, offsets, sequence)
                    except Exception:
                        logger.warning("log upload temporarily unavailable for %s", worker_job_id)
                    time.sleep(0.5)
                for _ in range(10):
                    try:
                        sequence = self._flush_log_events(row, offsets, sequence)
                        break
                    except Exception:
                        time.sleep(1)
            result = self.runtime.wait_process(worker_job_id)
            status = result["status"]
            error = "" if status in {"completed", "cancelled"} else f"process exited with {result['exit_code']}"
            if row:
                work_dir = Path(row["work_dir"])
                # 识别 tradefed "No matched tradefed modules"，把
                # "process exited with N" 的误导性错误替换为结构化根因。
                stdout_log = work_dir / "stdout.log"
                if status == "failed" and stdout_log.is_file():
                    try:
                        tail = stdout_log.read_text(
                            encoding="utf-8", errors="replace")[-256 * 1024:]
                    except OSError:
                        tail = ""
                    from .module_validation import detect_no_matched_modules

                    no_match_error = detect_no_matched_modules(tail)
                    if no_match_error:
                        error = no_match_error
                        result = {**result, "error": no_match_error}
                for log_name in ("stdout.log", "stderr.log"):
                    log_path = work_dir / log_name
                    if log_path.exists():
                        self._retry(lambda path=log_path: self.client.upload_artifact(
                            row["job_id"], row["attempt_id"], path, "log"))
                self._upload_tradefed_results(row, work_dir)
                # 失败任务自动采集设备现场证据（logcat + 快照），
                # 尽力而为，采集/上传失败不影响结果上报。
                if status == "failed":
                    try:
                        from .failure_evidence import collect_failure_evidence

                        serials = self._job_device_serials(row)
                        collect_failure_evidence(
                            row["job_id"], row["attempt_id"], serials, work_dir,
                            self.client.upload_artifact, self._retry,
                        )
                    except Exception:
                        logger.exception("failure evidence collection error")
            self.runtime.save_command(command_id, status, result, error)
            self._retry(lambda: self._ack_command(command_id, status, result, error))
        except Exception as exc:
            logger.exception("job monitor failed for %s", worker_job_id)
            self.runtime.save_command(command_id, "failed", error=str(exc))
            try:
                self._ack_command(command_id, "failed", error=str(exc))
            except Exception:
                logger.exception("failed to report job monitor failure")
        finally:
            if row:
                self.runtime.release_attempt_fencing(str(row["attempt_id"] or ""))

    def run_suite_action(self, command: dict):
        try:
            def report_progress(progress: dict):
                self.runtime.save_command(command["id"], "running", progress)
                try:
                    self._ack_command(command["id"], "running", progress)
                except Exception:
                    logger.debug("suite progress update failed", exc_info=True)

            result = execute_suite_action(self.config, command.get("payload", {}), report_progress)
            self.runtime.save_command(command["id"], "completed", result)
            self._retry(lambda: self._ack_command(command["id"], "completed", result))
            self.suites = scan_suites(self.config)
            self.last_suite_scan = time.monotonic()
        except Exception as exc:
            logger.exception("suite action %s failed", command.get("id"))
            error = str(exc)
            self.runtime.save_command(command["id"], "failed", error=error)
            self._retry(lambda: self._ack_command(command["id"], "failed", error=error))

    def run_usbip_action(self, command: dict):
        try:
            payload = command.get("payload", {})
            kind = command.get("command_type", "")
            with self.usbip_operation_lock:
                requested_generation = int(payload.get("generation") or 0)
                source_value = str(payload.get("source_host") or "")
                selected_busids = {str(item) for item in payload.get("busids") or []}
                newer = [
                    item for item in self.runtime.usbip_assignments()
                    if str(item.get("source_host") or "") == source_value
                    and str(item.get("busid") or "") in selected_busids
                    and int(item.get("generation") or 0) > requested_generation
                ]
                if newer:
                    raise TransportOperationError(
                        "TRANSPORT_STALE_GENERATION",
                        "已拒绝过期的USB/IP操作结果",
                        details={"generation": requested_generation},
                    )
                execute_args = [
                    "attach" if kind == "usbip_attach" else "detach",
                    str(payload.get("source_host") or ""),
                    list(payload.get("busids") or []),
                    str(payload.get("adb_server_socket") or "") or None,
                ]
                generation = int(payload.get("generation") or 0)
                result = (
                    execute_usbip_action(*execute_args, generation)
                    if generation
                    else execute_usbip_action(*execute_args)
                )
                source_host = str(payload.get("source_host") or "")
                busids = list(payload.get("busids") or [])
                if kind == "usbip_attach":
                    self.runtime.remember_usbip_assignments(
                        source_host,
                        busids,
                        str(payload.get("adb_server_socket") or ""),
                        int(payload.get("generation") or 0),
                    )
                else:
                    self.runtime.forget_usbip_assignments(source_host, busids)
                self.next_usbip_recovery_at = (
                    time.monotonic() + 60
                    if self.runtime.usbip_assignments()
                    else float("inf")
                )
            self.runtime.save_command(command["id"], "completed", result)
            self._retry(
                lambda: self._ack_command(
                    command["id"], "completed", result
                )
            )
        except Exception as exc:
            logger.exception("USB/IP command %s failed", command.get("id"))
            error = str(exc)
            error_result = (
                exc.as_dict() if isinstance(exc, TransportOperationError) else {}
            )
            self.runtime.save_command(
                command["id"], "failed", error_result, error=error
            )
            self._retry(
                lambda: self._ack_command(
                    command["id"], "failed", error_result, error=error
                )
            )
        finally:
            self.runtime.release_fencing(command)

    def recover_usbip_assignments(self) -> dict[str, list[str]]:
        recovered: list[str] = []
        errors: list[str] = []
        if not self.usbip_operation_lock.acquire(blocking=False):
            return {
                "recovered": [],
                "errors": ["USB/IP operation is already in progress"],
            }
        try:
            grouped: dict[tuple[str, str, int], list[str]] = {}
            for assignment in self.runtime.usbip_assignments():
                key = (
                    str(assignment.get("source_host") or ""),
                    str(assignment.get("adb_server_socket") or ""),
                    int(assignment.get("generation") or 0),
                )
                grouped.setdefault(key, []).append(
                    str(assignment.get("busid") or "")
                )
            for (
                source_host,
                adb_server_socket,
                generation,
            ), busids in grouped.items():
                selected = [busid for busid in busids if busid]
                if not source_host or not selected:
                    continue
                try:
                    execute_args = [
                        "attach", source_host, selected, adb_server_socket or None
                    ]
                    if generation:
                        execute_usbip_action(*execute_args, generation)
                    else:
                        execute_usbip_action(*execute_args)
                    recovered.extend(
                        f"{source_host}:{busid}" for busid in selected
                    )
                except Exception as exc:
                    errors.append(
                        f"{source_host} ({', '.join(selected)}): {exc}"
                    )
        finally:
            self.usbip_operation_lock.release()
        return {"recovered": recovered, "errors": errors}

    def run_suite_export(self, command: dict):
        path = None
        temporary = False
        try:
            payload = command.get("payload", {})
            path, temporary = prepare_suite_export(self.config, payload)
            self.client.upload_transfer(payload["transfer_id"], path)
            summary = {"transfer_id": payload["transfer_id"], "filename": path.name,
                       "size_bytes": path.stat().st_size}
            self.runtime.save_command(command["id"], "completed", summary)
            self._retry(lambda: self._ack_command(command["id"], "completed", summary))
        except Exception as exc:
            logger.exception("suite export %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            try:
                self._ack_command(command["id"], "failed", error=str(exc))
            except Exception:
                logger.exception("failed to acknowledge suite export failure")
        finally:
            if temporary and path is not None:
                path.unlink(missing_ok=True)

    def run_file_transfer(self, command: dict):
        """Upload one Worker-local file to a Controller transfer (cross-Worker GSI pull)."""
        try:
            payload = command.get("payload", {})
            source_path = Path(str(payload.get("source_path") or "")).expanduser()
            transfer_id = str(payload.get("transfer_id") or "")
            if not source_path.is_file():
                raise ValueError("source file not found on Worker")
            self.client.upload_transfer(transfer_id, source_path)
            summary = {"transfer_id": transfer_id, "filename": source_path.name,
                       "size_bytes": source_path.stat().st_size}
            self.runtime.save_command(command["id"], "completed", summary)
            self._retry(lambda: self._ack_command(command["id"], "completed", summary))
        except Exception as exc:
            logger.exception("file transfer %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            try:
                self._ack_command(command["id"], "failed", error=str(exc))
            except Exception:
                logger.exception("failed to acknowledge file transfer failure")

    def run_report_import(self, command: dict):
        directory = None
        try:
            payload = command.get("payload", {})
            transfer_id = str(payload.get("transfer_id") or "")
            if not re.fullmatch(r"transfer-[a-f0-9]{32}", transfer_id):
                raise ValueError("invalid report copy transfer")
            directory = self.config.data_root / "report-copies" / transfer_id
            directory.mkdir(parents=True, exist_ok=False)
            archive = directory / "report.zip"
            self.client.download(
                f"/api/cluster/workers/{self.config.worker_id}/report-copies/{transfer_id}",
                archive,
            )
            digest = hashlib.sha256()
            with archive.open("rb") as source:
                while block := source.read(4 * 1024 * 1024):
                    digest.update(block)
            if (
                archive.stat().st_size != int(payload.get("size_bytes") or 0)
                or digest.hexdigest() != str(payload.get("sha256") or "")
            ):
                raise ValueError("report archive checksum mismatch")
            result = import_suite_report(
                self.config,
                archive,
                str(payload.get("target_suite_path") or ""),
                str(payload.get("report_name") or ""),
            )
            self.runtime.save_command(command["id"], "completed", result)
            self._retry(lambda: self._ack_command(command["id"], "completed", result))
        except Exception as exc:
            logger.exception("report import %s failed", command.get("id"))
            error = str(exc)
            self.runtime.save_command(command["id"], "failed", error=error)
            try:
                self._ack_command(command["id"], "failed", error=error)
            except Exception:
                logger.exception("failed to acknowledge report import failure")
        finally:
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)

    def run_device_export(self, command: dict):
        path = None
        try:
            payload = command.get("payload", {})
            path = prepare_device_export(payload)
            self.client.upload_transfer(
                payload["transfer_id"], path, filename=Path(payload["path"]).name
            )
            summary = {
                "transfer_id": payload["transfer_id"],
                "filename": Path(payload["path"]).name,
                "size_bytes": path.stat().st_size,
                "device": payload["devices"][0],
            }
            self.runtime.save_command(command["id"], "completed", summary)
            self._retry(lambda: self._ack_command(command["id"], "completed", summary))
        except Exception as exc:
            logger.exception("device export %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            try:
                self._ack_command(command["id"], "failed", error=str(exc))
            except Exception:
                logger.exception("failed to acknowledge device export failure")
        finally:
            self.runtime.release_fencing(command)
            if path is not None:
                path.unlink(missing_ok=True)

    def run_firmware_flash(self, command: dict):
        directory = None
        uploader = None
        try:
            payload = command.get("payload", {})
            directory = self.config.data_root / "firmware" / payload["stage_id"]
            directory.mkdir(parents=True, exist_ok=False)
            import hashlib
            from urllib.parse import quote, urlencode
            specs = list(payload.get("files") or [])
            if not specs and "filename" in payload:
                specs = [{"filename": payload["filename"],
                          "size_bytes": payload["size_bytes"], "sha256": payload["sha256"],
                          "kind": "firmware"}]
            # local_sources: 镜像已在本 Worker 磁盘上（如集群模式从 Worker 目录
            # 选择 GSI），拷贝进烧写 staging 并校验后直接使用，无需回传 Controller。
            for source in payload.get("local_sources") or []:
                kind = str(source.get("kind") or "")
                if kind not in {"system", "vendor"}:
                    raise ValueError("invalid local GSI source kind")
                target = directory / f"{kind}.img"
                max_bytes = int(os.getenv(
                    "GMS_WORKER_GSI_STAGE_MAX_BYTES", str(20 * 1024 ** 3)))
                staged = copy_image_into(str(source.get("path") or ""), target, max_bytes)
                specs.append({"kind": kind, "filename": target.name, "local": True, **staged})
            downloaded = {}
            for spec in specs:
                target = directory / Path(spec["filename"]).name
                if spec.get("local"):
                    downloaded[spec["kind"]] = target
                    continue
                endpoint = (f"/api/cluster/workers/{quote(self.config.worker_id)}/firmware/"
                            f"{quote(payload['stage_id'])}?{urlencode({'filename': spec['filename']})}")
                self.client.download(endpoint, target)
                digest = hashlib.sha256()
                with target.open("rb") as source:
                    while block := source.read(4 * 1024 * 1024):
                        digest.update(block)
                if target.stat().st_size != int(spec["size_bytes"]) or digest.hexdigest() != spec["sha256"]:
                    raise ValueError("staged image checksum mismatch")
                downloaded[spec["kind"]] = target
            # 烧写过程逐行上报 command events，浏览器可实时
            # 看到 fastboot/upgrade_tool 输出（不再是结束后一次性 20KB）。
            # 输出回调只入队，HTTP 上传在独立 uploader 线程执行——
            # 日志上报绝不能阻塞 child process stdout 消费，
            # 否则 pipe 填满会把 fastboot/upgrade_tool 卡死。
            event_sequence = [0]
            event_sequence_lock = threading.Lock()
            uploader = CommandEventUploader(
                lambda batch: self.client.command_events(command["id"], batch))

            def _report_flash_output(line: str, _is_stderr: bool) -> None:
                with event_sequence_lock:
                    sequence = event_sequence[0]
                    event_sequence[0] += 1
                uploader.submit({
                    "sequence": sequence,
                    "event_type": "log",
                    "source": "burn",
                    "level": "info",
                    "message": line[:2000],
                    "payload": {},
                })

            result = (flash_gsi(self.config, downloaded.get("system"), downloaded.get("vendor"), payload.get("devices", []),
                                on_output=_report_flash_output)
                      if command["command_type"] == "flash_gsi" else
                      flash_firmware(self.config, downloaded["firmware"], payload.get("devices", []),
                                     on_output=_report_flash_output))
            uploader.flush()  # 收尾：有界等待排空剩余事件
            status = "completed" if result.get("success") else "failed"
            error = "" if status == "completed" else result.get("output", "firmware flash failed")
            self.runtime.save_command(command["id"], status, result, error)
            self._retry(lambda: self._ack_command(command["id"], status, result, error))
        except Exception as exc:
            logger.exception("firmware command %s failed", command.get("id"))
            error = str(exc)
            if uploader is not None:
                # 异常路径同样有界排空已产生的过程日志：烧写中途失败的
                # 输出证据不能随 daemon uploader 线程丢弃。
                uploader.flush()
            self.runtime.save_command(command["id"], "failed", error=error)
            self._retry(lambda: self._ack_command(command["id"], "failed", error=error))
        finally:
            self.runtime.release_fencing(command)
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)

    def _flush_log_events(self, row, offsets: dict[str, int], sequence: int) -> int:
        events = []
        new_offsets = dict(offsets)
        row_keys = set(row.keys())
        for log_name in ("stdout.log", "stderr.log"):
            path = Path(row["work_dir"]) / log_name
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offsets[log_name])
                # 限制长时间测试的日志转发内存占用。
                text = handle.read(int(os.getenv("GMS_WORKER_LOG_BATCH_CHARS", str(256 * 1024))))
                new_offsets[log_name] = handle.tell()
            for line in text.splitlines():
                events.append({"sequence": sequence, "event_type": "log",
                               "source": log_name.removesuffix(".log"),
                               "level": "error" if log_name == "stderr.log" else "info",
                               "message": line, "payload": {
                                   "job_id": row["job_id"],
                                   "attempt_id": row["attempt_id"],
                                   "trace_id": row["trace_id"] if "trace_id" in row_keys else "",
                                   "operation_id": (
                                       row["operation_id"]
                                       if "operation_id" in row_keys else ""
                                   ),
                                   "worker_id": self.config.worker_id,
                               }})
                sequence += 1
        if events:
            self.client.events(row["job_id"], row["attempt_id"], events)
            offsets.update(new_offsets)
        return sequence

    @staticmethod
    def _retry(action, attempts: int = 30, delay: float = 1.0):
        last_error = None
        for _ in range(attempts):
            try:
                return action()
            except Exception as exc:
                last_error = exc
                time.sleep(delay)
        raise last_error or RuntimeError("operation failed")

    def _job_device_serials(self, row) -> list[str]:
        """从 jobs 表的 devices_json 解析设备序列号。"""
        try:
            keys = set(row.keys())
            raw = row["devices_json"] if "devices_json" in keys else "[]"
            devices = json.loads(raw or "[]")
        except Exception as exc:
            # 解析失败不能静默：指纹/失败取证会因此整体缺失，至少留 warning。
            logger.warning(
                "failed to parse devices_json for job %s: %s",
                row.get("job_id") if isinstance(row, dict) else row,
                exc,
            )
            devices = []
        serials = []
        for item in devices if isinstance(devices, list) else []:
            serial = str(item or "").strip()
            # 形如 "worker:serial" 的前缀去掉（与 _device_serials 约定一致）。
            if ":" in serial:
                serial = serial.split(":", 1)[1]
            if serial:
                serials.append(serial)
        return serials

    def _report_device_fingerprints(self, row) -> int:
        """任务开始时上报设备指纹事件，返回已占用的 sequence 数。

        事件表 UNIQUE(attempt_id, sequence)：多台设备若都从 0 开始会互相
        静默覆盖（INSERT OR IGNORE），还会吃掉第一条 stdout 日志，因此
        sequence 必须连续递增。
        """
        used = 0
        try:
            from .failure_evidence import collect_device_fingerprint

            for serial in self._job_device_serials(row):
                fingerprint = collect_device_fingerprint(serial)
                if not any(fingerprint.values()):
                    continue
                self._retry(lambda s=serial, f=fingerprint, seq=used: self.client.events(
                    row["job_id"], row["attempt_id"],
                    [{
                        "sequence": seq,
                        "event_type": "device_fingerprint",
                        "source": "worker",
                        "level": "info",
                        "message": f"device fingerprint for {s}",
                        "payload": {"serial": s, **f},
                    }],
                ))
                used += 1
        except Exception:
            logger.exception("device fingerprint report failed")
        return used

    _RESULT_DIR_RE = re.compile(r"RESULT DIRECTORY\s*:\s*(\S+)")
    _FINAL_LOG_RE = re.compile(r"process final logs:\s*(/\S+)")
    _RESULT_PATH_RE = re.compile(
        r"(/[^\s]+/results/\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+_\d+)?)"
    )

    def _find_tradefed_result_dir(self, stdout: str) -> Path | None:
        """Locate the Tradefed result directory from stdout.

        CTS/GTS print ``RESULT DIRECTORY:`` directly. VTS instead prints
        ``process final logs:`` pointing at a temp inv directory whose
        ``end_host_log_*.txt`` file contains the real results path.
        """
        # 1. CTS/GTS — direct RESULT DIRECTORY line
        for match in reversed(self._RESULT_DIR_RE.findall(stdout)):
            path = Path(match).expanduser().resolve()
            if path.is_dir():
                return path

        # 2. VTS — follow process final logs into the inv dir, then read the
        #    end_host_log file for the actual results/<timestamp> path.
        for match in reversed(self._FINAL_LOG_RE.findall(stdout)):
            final_path = Path(match).expanduser().resolve()
            result = self._result_dir_from_host_log(final_path)
            if result:
                return result
            inv_dir = final_path.parent if final_path.is_file() else final_path
            if inv_dir.is_dir():
                result = self._result_dir_from_inv_dir(inv_dir)
                if result:
                    return result
        return None

    def _result_dir_from_host_log(self, log_path: Path) -> Path | None:
        if not log_path.is_file():
            return None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for match in self._RESULT_PATH_RE.finditer(text):
            candidate = Path(match.group(1)).resolve()
            if candidate.is_dir():
                return candidate
        return None

    def _result_dir_from_inv_dir(self, inv_dir: Path) -> Path | None:
        try:
            entries = list(inv_dir.iterdir())
        except OSError:
            return None
        for entry in entries:
            if entry.name.startswith("end_host_log_") and entry.suffix == ".txt":
                result = self._result_dir_from_host_log(entry)
                if result:
                    return result
        return None

    def _upload_tradefed_results(self, row, work_dir: Path) -> None:
        stdout_path = work_dir / "stdout.log"
        with stdout_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4 * 1024 * 1024))
            stdout = handle.read().decode("utf-8", errors="replace")
        result_dir = self._find_tradefed_result_dir(stdout)
        if result_dir is None:
            # row 可能是 sqlite3.Row（无 .get）；用下标访问并容忍缺列。
            # sqlite3.Row 的 __contains__ 匹配列值而非列名，必须用 .keys()。
            logger.info(
                "no tradefed result directory found in stdout for job %s",
                row["job_id"] if "job_id" in row.keys() else "<unknown>",  # noqa: SIM118
            )
            return
        if not any(
            root.exists() and result_dir.is_relative_to(root.resolve())
            for root in self.config.suite_roots
        ):
            logger.warning("ignored result directory outside suite roots: %s", result_dir)
            return
        # VTS does not print "RESULT DIRECTORY:" in stdout; append it so the
        # controller can resolve the report name from the uploaded stdout.log.
        if "RESULT DIRECTORY" not in stdout:
            with stdout_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\nRESULT DIRECTORY: {result_dir}\n")
            self._retry(lambda: self.client.upload_artifact(
                row["job_id"], row["attempt_id"], stdout_path, "log"))
        for name in ("test_result.xml", "test_result.html", "test_result_failures.html"):
            path = result_dir / name
            if path.is_file():
                self._retry(lambda p=path: self.client.upload_artifact(
                    row["job_id"], row["attempt_id"], p, "report"))
        archive_base = work_dir / "tradefed-results"
        archive_path = Path(shutil.make_archive(str(archive_base), "zip", result_dir))
        self._retry(lambda: self.client.upload_artifact(
            row["job_id"], row["attempt_id"], archive_path, "report-archive"))

    def run(self):
        registered = False
        recovered = False
        next_heartbeat = 0.0
        while True:
            try:
                if not registered:
                    self.client.register(self.registration())
                    # 注册后的首次心跳强制重新上报设备清单。
                    self.last_suite_scan = 0.0
                    registered = True
                    logger.info("registered as %s", self.config.worker_id)
                if not recovered:
                    recoverable_jobs = self.runtime.recoverable_jobs()
                    for command in self.runtime.fail_interrupted_commands():
                        self._retry(lambda item=command: self._ack_command(
                            item["id"], item["status"], item["result"], item["error"]
                        ))
                    for job in recoverable_jobs:
                        threading.Thread(target=self.monitor_recovered_job, args=(job,),
                                         name=f"Recovered-{job['worker_job_id']}", daemon=True).start()
                    recovered = True
                if time.monotonic() >= next_heartbeat:
                    self.heartbeat()
                    next_heartbeat = time.monotonic() + self.config.heartbeat_interval
                for command in self.client.poll():
                    self.handle(command)
            except KeyboardInterrupt:
                return
            except Exception:
                logger.exception("controller loop failed")
                registered = False
                time.sleep(5)

    def monitor_recovered_job(self, job: dict):
        row = {"job_id": job["job_id"], "attempt_id": job["attempt_id"],
               "work_dir": job["work_dir"], "trace_id": job.get("trace_id", ""),
               "operation_id": job.get("operation_id", "")}
        offsets = {"stdout.log": 0, "stderr.log": 0}
        sequence = 0
        while self.runtime.pid_alive(int(job["pid"])):
            try:
                sequence = self._flush_log_events(row, offsets, sequence)
            except Exception:
                logger.warning("recovered log upload unavailable for %s", job["worker_job_id"])
            time.sleep(0.5)
        for _ in range(10):
            if (Path(job["work_dir"]) / "exit_code").exists():
                break
            time.sleep(0.2)
        result = self.runtime.finish_recovered_job(job["worker_job_id"])
        status = result["status"]
        error = "" if status == "completed" else f"recovered process exited with {result['exit_code']}"
        work_dir = Path(job["work_dir"])
        for log_name in ("stdout.log", "stderr.log"):
            path = work_dir / log_name
            if path.exists():
                self._retry(lambda p=path: self.client.upload_artifact(
                    job["job_id"], job["attempt_id"], p, "log"))
        self._upload_tradefed_results(row, work_dir)
        self.runtime.save_command(job["command_id"], status, result, error)
        self._retry(lambda: self._ack_command(job["command_id"], status, result, error))
        self.runtime.release_attempt_fencing(str(job.get("attempt_id") or ""))


def main():
    """Backward-compatible shim; deployment should use ``worker_agent.entrypoint``."""
    from .entrypoint import main as entrypoint_main
    entrypoint_main()


if __name__ == "__main__":
    main()
