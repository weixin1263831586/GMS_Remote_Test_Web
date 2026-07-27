from __future__ import annotations

import getpass
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

from .android_inspection import _aapt2_path, prepare_device_export
from .client import ControllerClient
from .config import WorkerConfig
from .inventory import (
    execute_device_action,
    execute_suite_action,
    execute_usbip_action,
    flash_firmware,
    flash_gsi,
    host_metrics,
    prepare_suite_export,
    probe_devices,
    scan_suites,
)
from .process_inventory import discover_tradefed_processes
from .runtime import WorkerRuntime


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gms-worker")
AGENT_VERSION = "0.5.0"


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


class WorkerAgent:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.client = ControllerClient(config)
        self.runtime = WorkerRuntime(config)
        self.session_id = f"worker-session-{uuid.uuid4().hex}"
        self.suites = []
        self.last_suite_scan = 0.0

    def registration(self):
        try:
            _aapt2_path()
            has_aapt2 = True
        except RuntimeError:
            has_aapt2 = False
        capabilities = {"adb": shutil.which("adb") is not None,
                        "fastboot": shutil.which("fastboot") is not None,
                        "tradefed": True,
                        "cts": True, "gts": True, "vts": True, "sts": True,
                        "device_inspection": True,
                        "usbip_client": (
                            shutil.which("usbip") is not None
                            and os.access("/usr/local/libexec/gms-worker-usbip", os.X_OK)
                        ),
                        "aapt2": has_aapt2,
                        "ssh_user": self.config.ssh_user or getpass.getuser()}
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
            self._ack_command(command["id"], previous["status"], previous["result"], previous["error"])
            return
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
            self._ack_command(command["id"], "completed", result)
        except Exception as exc:
            logger.exception("command %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
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
        try:
            # Resolve controller identifiers from the durable jobs table.
            with self.runtime.connect() as conn:
                row = conn.execute(
                    """SELECT job_id,attempt_id,work_dir,trace_id,operation_id
                       FROM jobs WHERE worker_job_id=?""",
                                   (worker_job_id,)).fetchone()
            if row:
                sequence = 0
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
                for log_name in ("stdout.log", "stderr.log"):
                    log_path = work_dir / log_name
                    if log_path.exists():
                        self._retry(lambda path=log_path: self.client.upload_artifact(
                            row["job_id"], row["attempt_id"], path, "log"))
                self._upload_tradefed_results(row, work_dir)
            self.runtime.save_command(command_id, status, result, error)
            self._retry(lambda: self._ack_command(command_id, status, result, error))
        except Exception as exc:
            logger.exception("job monitor failed for %s", worker_job_id)
            self.runtime.save_command(command_id, "failed", error=str(exc))
            try:
                self._ack_command(command_id, "failed", error=str(exc))
            except Exception:
                logger.exception("failed to report job monitor failure")

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
            result = execute_usbip_action(
                "attach" if kind == "usbip_attach" else "detach",
                str(payload.get("source_host") or ""),
                list(payload.get("busids") or []),
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
            self.runtime.save_command(command["id"], "failed", error=error)
            self._retry(
                lambda: self._ack_command(
                    command["id"], "failed", error=error
                )
            )

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
            if path is not None:
                path.unlink(missing_ok=True)

    def run_firmware_flash(self, command: dict):
        directory = None
        try:
            payload = command.get("payload", {})
            directory = self.config.data_root / "firmware" / payload["stage_id"]
            directory.mkdir(parents=True, exist_ok=False)
            import hashlib
            from urllib.parse import quote, urlencode
            specs = payload.get("files") or [{"filename": payload["filename"],
                "size_bytes": payload["size_bytes"], "sha256": payload["sha256"], "kind": "firmware"}]
            downloaded = {}
            for spec in specs:
                target = directory / Path(spec["filename"]).name
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
            result = (flash_gsi(self.config, downloaded["system"], downloaded.get("vendor"), payload.get("devices", []))
                      if command["command_type"] == "flash_gsi" else
                      flash_firmware(self.config, downloaded["firmware"], payload.get("devices", [])))
            status = "completed" if result.get("success") else "failed"
            error = "" if status == "completed" else result.get("output", "firmware flash failed")
            self.runtime.save_command(command["id"], status, result, error)
            self._retry(lambda: self._ack_command(command["id"], status, result, error))
        except Exception as exc:
            logger.exception("firmware command %s failed", command.get("id"))
            error = str(exc)
            self.runtime.save_command(command["id"], "failed", error=error)
            self._retry(lambda: self._ack_command(command["id"], "failed", error=error))
        finally:
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

    def _upload_tradefed_results(self, row, work_dir: Path) -> None:
        stdout_path = work_dir / "stdout.log"
        with stdout_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4 * 1024 * 1024))
            stdout = handle.read().decode("utf-8", errors="replace")
        matches = re.findall(r"RESULT DIRECTORY\s*:\s*(\S+)", stdout)
        if not matches:
            return
        result_dir = Path(matches[-1]).expanduser().resolve()
        if not result_dir.is_dir() or not any(
            root.exists() and result_dir.is_relative_to(root.resolve())
            for root in self.config.suite_roots
        ):
            logger.warning("ignored result directory outside suite roots: %s", result_dir)
            return
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


def main():
    WorkerAgent(WorkerConfig.load()).run()


if __name__ == "__main__":
    main()
