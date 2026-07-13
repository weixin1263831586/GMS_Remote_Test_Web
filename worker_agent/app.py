from __future__ import annotations

import logging
import getpass
import socket
import threading
import time
import re
import shutil
from pathlib import Path
from datetime import datetime, timezone

from .client import ControllerClient
from .config import WorkerConfig
from .inventory import (execute_device_action, execute_suite_action, flash_firmware, flash_gsi, host_metrics,
                        prepare_suite_export, probe_devices, scan_suites)
from .runtime import WorkerRuntime


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gms-worker")
AGENT_VERSION = "0.1.0"


class WorkerAgent:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.client = ControllerClient(config)
        self.runtime = WorkerRuntime(config)
        self.suites = []
        self.last_suite_scan = 0.0

    def registration(self):
        return {"worker_id": self.config.worker_id, "name": self.config.name,
                "hostname": socket.gethostname(),
                "address": self.config.address or socket.gethostname(),
                "agent_version": AGENT_VERSION, "max_jobs": self.config.max_jobs,
                "capabilities": {"adb": True, "fastboot": True, "tradefed": True,
                                 "cts": True, "gts": True, "vts": True, "sts": True,
                                 "ssh_user": self.config.ssh_user or getpass.getuser(),
                                 "novnc_port": 6080}}

    def heartbeat(self):
        now = time.monotonic()
        include_suites = not self.suites or now - self.last_suite_scan >= self.config.suite_scan_interval
        if include_suites:
            self.suites = scan_suites(self.config)
            self.last_suite_scan = now
        payload = {"agent_version": AGENT_VERSION, **host_metrics(self.config),
                   "running_jobs": self.runtime.running_jobs(), "devices": probe_devices(),
                   "timestamp": datetime.now(timezone.utc).isoformat()}
        if include_suites:
            payload["suites"] = self.suites
        self.client.heartbeat(payload)

    def handle(self, command):
        previous = self.runtime.previous_command(command["id"])
        if previous and previous["status"] in {"completed", "failed", "cancelled"}:
            self.client.ack(command["id"], previous["status"], previous["result"], previous["error"])
            return
        try:
            kind = command["command_type"]
            if kind == "refresh_devices":
                result = {"devices": probe_devices()}
            elif kind == "refresh_suites":
                self.suites = scan_suites(self.config)
                self.last_suite_scan = time.monotonic()
                result = {"suites": self.suites}
            elif kind == "device_action":
                payload = command.get("payload", {})
                result = execute_device_action(payload.get("action", ""), payload.get("devices", []), payload)
            elif kind == "suite_action":
                if command.get("payload", {}).get("action") in {"download_url", "extract"}:
                    self.runtime.save_command(command["id"], "running", {})
                    self.client.ack(command["id"], "running", {})
                    threading.Thread(target=self.run_suite_action,
                                     args=(command,), name=f"SuiteAction-{command['id']}", daemon=True).start()
                    return
                result = execute_suite_action(self.config, command.get("payload", {}))
            elif kind == "suite_export":
                self.runtime.save_command(command["id"], "running", {})
                self.client.ack(command["id"], "running", {})
                threading.Thread(target=self.run_suite_export,
                                 args=(command,), name=f"SuiteExport-{command['id']}", daemon=True).start()
                return
            elif kind in {"flash_firmware", "flash_gsi"}:
                self.runtime.save_command(command["id"], "running", {})
                self.client.ack(command["id"], "running", {})
                threading.Thread(target=self.run_firmware_flash, args=(command,),
                                 name=f"Firmware-{command['id']}", daemon=True).start()
                return
            elif kind == "start_test":
                result = self.runtime.start_process(command)
                self.runtime.save_command(command["id"], "running", result)
                self.client.ack(command["id"], "running", result)
                threading.Thread(
                    target=self.monitor_job,
                    args=(command["id"], result["worker_job_id"]),
                    name=f"JobMonitor-{result['worker_job_id']}", daemon=True,
                ).start()
                return
            elif kind == "stop_test":
                result = self.runtime.stop_process(command.get("payload", {}).get("worker_job_id", ""))
            else:
                raise ValueError(f"unsupported command type: {kind}")
            self.runtime.save_command(command["id"], "completed", result)
            self.client.ack(command["id"], "completed", result)
        except Exception as exc:
            logger.exception("command %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            self.client.ack(command["id"], "failed", error=str(exc))

    def monitor_job(self, command_id: str, worker_job_id: str):
        try:
            # Resolve controller identifiers from the durable jobs table.
            with self.runtime.connect() as conn:
                row = conn.execute("SELECT job_id,attempt_id,work_dir FROM jobs WHERE worker_job_id=?",
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
                        self._retry(lambda: self.client.upload_artifact(
                            row["job_id"], row["attempt_id"], log_path, "log"))
                self._upload_tradefed_results(row, work_dir)
            self.runtime.save_command(command_id, status, result, error)
            self._retry(lambda: self.client.ack(command_id, status, result, error))
        except Exception as exc:
            logger.exception("job monitor failed for %s", worker_job_id)
            self.runtime.save_command(command_id, "failed", error=str(exc))
            try:
                self.client.ack(command_id, "failed", error=str(exc))
            except Exception:
                logger.exception("failed to report job monitor failure")

    def run_suite_action(self, command: dict):
        try:
            def report_progress(progress: dict):
                self.runtime.save_command(command["id"], "running", progress)
                try:
                    self.client.ack(command["id"], "running", progress)
                except Exception:
                    logger.debug("suite progress update failed", exc_info=True)

            result = execute_suite_action(self.config, command.get("payload", {}), report_progress)
            self.runtime.save_command(command["id"], "completed", result)
            self._retry(lambda: self.client.ack(command["id"], "completed", result))
            self.suites = scan_suites(self.config)
            self.last_suite_scan = time.monotonic()
        except Exception as exc:
            logger.exception("suite action %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            self._retry(lambda: self.client.ack(command["id"], "failed", error=str(exc)))

    def run_suite_export(self, command: dict):
        path = None
        temporary = False
        try:
            payload = command.get("payload", {})
            path, temporary = prepare_suite_export(self.config, payload)
            result = self.client.upload_transfer(payload["transfer_id"], path)
            summary = {"transfer_id": payload["transfer_id"], "filename": path.name,
                       "size_bytes": path.stat().st_size}
            self.runtime.save_command(command["id"], "completed", summary)
            self._retry(lambda: self.client.ack(command["id"], "completed", summary))
        except Exception as exc:
            logger.exception("suite export %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            try:
                self.client.ack(command["id"], "failed", error=str(exc))
            except Exception:
                logger.exception("failed to acknowledge suite export failure")
        finally:
            if temporary and path is not None:
                path.unlink(missing_ok=True)

    def run_firmware_flash(self, command: dict):
        try:
            payload = command.get("payload", {})
            directory = self.config.data_root / "firmware" / payload["stage_id"]
            directory.mkdir(parents=True, exist_ok=False)
            from urllib.parse import quote, urlencode
            import hashlib
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
                    while block := source.read(4 * 1024 * 1024): digest.update(block)
                if target.stat().st_size != int(spec["size_bytes"]) or digest.hexdigest() != spec["sha256"]:
                    raise ValueError("staged image checksum mismatch")
                downloaded[spec["kind"]] = target
            result = (flash_gsi(self.config, downloaded["system"], downloaded.get("vendor"), payload.get("devices", []))
                      if command["command_type"] == "flash_gsi" else
                      flash_firmware(self.config, downloaded["firmware"], payload.get("devices", [])))
            status = "completed" if result.get("success") else "failed"
            error = "" if status == "completed" else result.get("output", "firmware flash failed")
            self.runtime.save_command(command["id"], status, result, error)
            self._retry(lambda: self.client.ack(command["id"], status, result, error))
        except Exception as exc:
            logger.exception("firmware command %s failed", command.get("id"))
            self.runtime.save_command(command["id"], "failed", error=str(exc))
            self._retry(lambda: self.client.ack(command["id"], "failed", error=str(exc)))

    def _flush_log_events(self, row, offsets: dict[str, int], sequence: int) -> int:
        events = []
        new_offsets = dict(offsets)
        for log_name in ("stdout.log", "stderr.log"):
            path = Path(row["work_dir"]) / log_name
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offsets[log_name])
                text = handle.read()
                new_offsets[log_name] = handle.tell()
            for line in text.splitlines():
                events.append({"sequence": sequence, "event_type": "log",
                               "source": log_name.removesuffix(".log"),
                               "level": "error" if log_name == "stderr.log" else "info",
                               "message": line, "payload": {}})
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
        stdout = (work_dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
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
                    registered = True
                    logger.info("registered as %s", self.config.worker_id)
                if not recovered:
                    for job in self.runtime.recoverable_jobs():
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
               "work_dir": job["work_dir"]}
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
        self._retry(lambda: self.client.ack(job["command_id"], status, result, error))


def main():
    WorkerAgent(WorkerConfig.load()).run()


if __name__ == "__main__":
    main()
