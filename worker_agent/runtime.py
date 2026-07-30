from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .config import WorkerConfig


class WorkerRuntime:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.config.data_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config.data_root / "worker.sqlite3"
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, result_json TEXT NOT NULL,
                error TEXT NOT NULL, controller_synced INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                worker_job_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                pid INTEGER, pgid INTEGER, status TEXT NOT NULL, devices_json TEXT NOT NULL,
                work_dir TEXT NOT NULL, exit_code INTEGER, error TEXT NOT NULL,
                command_id TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL DEFAULT '',
                operation_id TEXT NOT NULL DEFAULT '')""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "command_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN command_id TEXT NOT NULL DEFAULT ''")
            if "trace_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")
            if "operation_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN operation_id TEXT NOT NULL DEFAULT ''")
            command_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(commands)").fetchall()
            }
            if "controller_synced" not in command_columns:
                conn.execute(
                    "ALTER TABLE commands ADD COLUMN controller_synced INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute("""CREATE TABLE IF NOT EXISTS device_fences (
                device_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                lease_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def previous_command(self, command_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "result": json.loads(row["result_json"] or "{}")}

    def save_command(self, command_id: str, status: str, result=None, error: str = ""):
        with self.connect() as conn:
            conn.execute("""INSERT INTO commands(id,status,result_json,error) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                result_json=excluded.result_json,error=excluded.error,
                controller_synced=0,updated_at=CURRENT_TIMESTAMP""",
                (command_id, status, json.dumps(result or {}, separators=(",", ":")), error))

    def mark_command_synced(self, command_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE commands SET controller_synced=1 WHERE id=?", (command_id,)
            )

    def mark_commands_synced(self, command_ids: list[str]) -> None:
        if not command_ids:
            return
        with self.connect() as conn:
            conn.executemany(
                "UPDATE commands SET controller_synced=1 WHERE id=?",
                [(command_id,) for command_id in command_ids],
            )

    def unsynced_commands(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id,status,result_json,error FROM commands
                   WHERE controller_synced=0
                   ORDER BY updated_at,id LIMIT ?""",
                (max(1, min(int(limit or 200), 200)),),
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            result.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "result": payload,
                    "error": row["error"],
                }
            )
        return result

    def validate_fencing(self, command: dict[str, Any]) -> None:
        payload = command.get("payload") or {}
        tokens = payload.get("lease_tokens") or []
        command_type = str(command.get("command_type") or "")
        protected_types = {
            "start_test", "device_action", "flash_firmware", "flash_gsi",
            "device_export", "usbip_detach",
        }
        require_fencing = os.getenv(
            "GMS_WORKER_REQUIRE_DEVICE_FENCING", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        requested_devices = {
            value if value.startswith(f"{self.config.worker_id}:")
            else f"{self.config.worker_id}:{value}"
            for item in payload.get("devices") or []
            if (value := str(item or "").strip())
        }
        if require_fencing and command_type in protected_types and requested_devices:
            if not tokens:
                raise ValueError(
                    f"{command_type} requires a valid device fencing token"
                )
            token_devices = {
                str(token.get("device_id") or "") for token in tokens
            }
            if token_devices != requested_devices:
                raise ValueError("device fencing tokens do not match requested devices")
        if not tokens:
            return
        revoked_attempts: set[str] = set()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for token in tokens:
                device_id = str(token.get("device_id") or "")
                lease_id = str(token.get("lease_id") or "")
                attempt_id = str(token.get("attempt_id") or "")
                generation = int(token.get("generation") or 0)
                if not device_id or not lease_id or not attempt_id or generation <= 0:
                    raise ValueError("invalid device fencing token")
                current = conn.execute(
                    "SELECT * FROM device_fences WHERE device_id=?", (device_id,)
                ).fetchone()
                if current and (
                    generation < int(current["generation"])
                    or (
                        generation == int(current["generation"])
                        and (
                            current["lease_id"] != lease_id
                            or current["attempt_id"] != attempt_id
                        )
                    )
                ):
                    raise ValueError(
                        f"stale fencing token for device {device_id}: "
                        f"generation {generation}, current generation "
                        f"{int(current['generation'])}"
                    )
                if current and generation > int(current["generation"]):
                    revoked_attempts.add(str(current["attempt_id"] or ""))
                conn.execute(
                    """INSERT INTO device_fences
                       (device_id,generation,lease_id,attempt_id,updated_at)
                       VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                       ON CONFLICT(device_id) DO UPDATE SET
                           generation=excluded.generation,
                           lease_id=excluded.lease_id,
                           attempt_id=excluded.attempt_id,
                           updated_at=CURRENT_TIMESTAMP""",
                    (device_id, generation, lease_id, attempt_id),
                )
        for attempt_id in revoked_attempts:
            if attempt_id:
                self.revoke_attempt(
                    attempt_id,
                    "superseded by a newer device fencing generation",
                )

    def release_fencing(self, command: dict[str, Any]) -> int:
        """Release only fences owned by the command's exact lease tokens."""
        tokens = (command.get("payload") or {}).get("lease_tokens") or []
        removed = 0
        with self.connect() as conn:
            for token in tokens:
                try:
                    generation = int(token.get("generation") or 0)
                except (TypeError, ValueError):
                    continue
                cursor = conn.execute(
                    """DELETE FROM device_fences
                       WHERE device_id=? AND generation=?
                       AND lease_id=? AND attempt_id=?""",
                    (
                        str(token.get("device_id") or ""),
                        generation,
                        str(token.get("lease_id") or ""),
                        str(token.get("attempt_id") or ""),
                    ),
                )
                removed += cursor.rowcount
        return removed

    def release_attempt_fencing(self, attempt_id: str) -> int:
        if not attempt_id:
            return 0
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM device_fences WHERE attempt_id=?",
                (attempt_id,),
            )
        return cursor.rowcount

    def prune_inactive_fences(self) -> int:
        """Remove stale fences while preserving every currently running job."""
        with self.connect() as conn:
            cursor = conn.execute(
                """DELETE FROM device_fences
                   WHERE attempt_id NOT IN (
                       SELECT attempt_id FROM jobs WHERE status='running'
                   )"""
            )
        return cursor.rowcount

    def revoke_attempt(self, attempt_id: str, reason: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT worker_job_id,pid,pgid FROM jobs
                   WHERE attempt_id=? AND status='running'""",
                (attempt_id,),
            ).fetchall()
        revoked = []
        for row in rows:
            try:
                os.killpg(int(row["pgid"]), signal.SIGINT)
            except ProcessLookupError:
                pass
            revoked.append(row["worker_job_id"])
        with self.connect() as conn:
            conn.execute(
                """UPDATE jobs SET status='cancelled',error=?
                   WHERE attempt_id=? AND status='running'""",
                (reason, attempt_id),
            )
        return revoked

    def fail_interrupted_commands(self) -> list[dict[str, Any]]:
        """Fail non-recoverable background commands left running by a restart.

        Managed Tradefed commands are recovered through the jobs table. A
        firmware/suite/export thread cannot be resumed safely, so acknowledge
        it as failed instead of leaving the Controller and its reservation
        stuck forever.
        """
        error = (
            "Worker Agent restarted while the command was running; "
            "the operation was not resumed and device state must be verified"
        )
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM commands WHERE status='running'
                   AND id NOT IN (
                       SELECT command_id FROM jobs
                       WHERE status='running' AND command_id!=''
                   )"""
            ).fetchall()
            conn.executemany(
                """UPDATE commands SET status='failed',error=?,
                   controller_synced=0,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='running'""",
                [(error, row["id"]) for row in rows],
            )
        return [
            {"id": row["id"], "status": "failed", "result": {}, "error": error}
            for row in rows
        ]

    def running_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        result = []
        now = time.time()
        for row in rows:
            work_dir = Path(row["work_dir"])
            log_mtimes = [path.stat().st_mtime for path in
                          (work_dir / "stdout.log", work_dir / "stderr.log") if path.exists()]
            log_age = int(max(0, now - max(log_mtimes))) if log_mtimes else None
            stall_seconds = int(os.getenv("GMS_TF_STALL_SECONDS", "3600"))
            warning = (f"Worker log has not changed for {log_age} seconds"
                       if log_age is not None and log_age >= stall_seconds else "")
            result.append({"worker_job_id": row["worker_job_id"], "job_id": row["job_id"],
                           "attempt_id": row["attempt_id"], "pid": row["pid"],
                           "status": row["status"], "devices": json.loads(row["devices_json"]),
                           "source": "managed", "log_path": str(work_dir / "stdout.log"),
                           "trace_id": row["trace_id"],
                           "operation_id": row["operation_id"],
                           "last_output_age_seconds": log_age, "warning": warning})
        return result

    def _ensure_test_script(self, executable: Path) -> None:
        """Copy run_GMS_Test_Auto.sh from the Worker install dir if missing.

        The script is installed to both ``INSTALL_ROOT/scripts/`` and the
        suite root during deployment. If the suite-root copy is later removed
        (manual cleanup, directory rebuild), restore it from the install dir
        so test execution does not fail with "executable not found".
        """
        if executable.name != "run_GMS_Test_Auto.sh":
            return
        # INSTALL_ROOT is two levels above this module: worker_agent/ → install root.
        install_script = Path(__file__).resolve().parent.parent / "scripts" / "run_GMS_Test_Auto.sh"
        if not install_script.is_file():
            return
        try:
            executable.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(install_script, executable)
            executable.chmod(0o755)
        except OSError:
            pass

    def start_process(self, command: dict[str, Any]) -> dict[str, Any]:
        payload = command.get("payload", {})
        argv = payload.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError("start_test payload requires argv string list")
        executable = Path(argv[0]).resolve()
        allowed = any(root.exists() and executable.is_relative_to(root.resolve())
                      for root in self.config.suite_roots)
        if not allowed:
            raise ValueError("test executable is outside configured suite roots")
        if not executable.is_file():
            self._ensure_test_script(executable)
        if not executable.is_file():
            configured = ", ".join(str(r) for r in self.config.suite_roots)
            raise ValueError(
                f"test executable not found: {executable}. "
                f"Ensure run_GMS_Test_Auto.sh is deployed to a suite root ({configured})."
            )
        worker_job_id = payload.get("worker_job_id") or f"wj-{command['id']}"
        work_dir = self.config.data_root / "jobs" / (command.get("job_id") or command["id"]) / (command.get("attempt_id") or "1")
        work_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (payload.get("env") or {}).items()})
        exit_code_path = work_dir / "exit_code"
        wrapper = '"$@"; rc=$?; printf "%s" "$rc" > "$GMS_EXIT_CODE_FILE"; exit "$rc"'
        env["GMS_EXIT_CODE_FILE"] = str(exit_code_path)
        # 子进程已复制描述符，父进程立即关闭自身副本。
        with (work_dir / "stdout.log").open("ab", buffering=0) as stdout_file, \
                (work_dir / "stderr.log").open("ab", buffering=0) as stderr_file:
            process = subprocess.Popen(
                ["/bin/bash", "-c", wrapper, "gms-worker-job", *argv],
                cwd=payload.get("cwd") or str(executable.parent),
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        with self._lock:
            self._processes[worker_job_id] = process
        with self.connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO jobs
                (worker_job_id,job_id,attempt_id,pid,pgid,status,devices_json,
                 work_dir,exit_code,error,command_id,trace_id,operation_id)
                 VALUES(?,?,?,?,?,'running',?,?,NULL,'',?,?,?)""",
                (worker_job_id, command.get("job_id", ""), command.get("attempt_id", ""),
                 process.pid, os.getpgid(process.pid), json.dumps(payload.get("devices", [])),
                 str(work_dir), command["id"], command.get("trace_id", ""),
                 command.get("operation_id", "")))
        return {"worker_job_id": worker_job_id, "pid": process.pid, "work_dir": str(work_dir)}

    def stop_process(self, worker_job_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE worker_job_id=?", (worker_job_id,)).fetchone()
        if not row:
            raise ValueError("worker job not found")
        if row["status"] != "running":
            return {"worker_job_id": worker_job_id, "status": row["status"]}
        try:
            os.killpg(int(row["pgid"]), signal.SIGINT)
        except ProcessLookupError:
            pass
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status='cancelled' WHERE worker_job_id=?", (worker_job_id,))
        return {"worker_job_id": worker_job_id, "status": "cancelled"}

    def wait_process(self, worker_job_id: str) -> dict[str, Any]:
        with self._lock:
            process = self._processes.get(worker_job_id)
        if process is None:
            raise ValueError("worker process is not attached to this agent instance")
        exit_code = process.wait()
        with self.connect() as conn:
            row = conn.execute("SELECT work_dir FROM jobs WHERE worker_job_id=?", (worker_job_id,)).fetchone()
            existing = conn.execute("SELECT status FROM jobs WHERE worker_job_id=?", (worker_job_id,)).fetchone()
            status = "cancelled" if existing and existing["status"] == "cancelled" else (
                "completed" if exit_code == 0 else "failed"
            )
            conn.execute("UPDATE jobs SET status=?,exit_code=? WHERE worker_job_id=?",
                         (status, exit_code, worker_job_id))
        with self._lock:
            self._processes.pop(worker_job_id, None)
        return {"worker_job_id": worker_job_id, "status": status,
                "exit_code": exit_code, "work_dir": row["work_dir"] if row else ""}

    def process_poll(self, worker_job_id: str) -> int | None:
        with self._lock:
            process = self._processes.get(worker_job_id)
        if process is None:
            raise ValueError("worker process is not attached to this agent instance")
        return process.poll()

    @staticmethod
    def pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def recoverable_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if self.pid_alive(int(item["pid"])) or (Path(item["work_dir"]) / "exit_code").exists():
                result.append(item)
            else:
                with self.connect() as conn:
                    conn.execute("UPDATE jobs SET status='failed',error='process missing after agent restart' WHERE worker_job_id=?",
                                 (item["worker_job_id"],))
        return result

    def finish_recovered_job(self, worker_job_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE worker_job_id=?", (worker_job_id,)).fetchone()
        if not row:
            raise ValueError("worker job not found")
        exit_path = Path(row["work_dir"]) / "exit_code"
        exit_code = int(exit_path.read_text().strip()) if exit_path.exists() else -1
        status = "completed" if exit_code == 0 else "failed"
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status=?,exit_code=?,error=? WHERE worker_job_id=?",
                         (status, exit_code, "" if status == "completed" else "recovered process failed", worker_job_id))
        return {"worker_job_id": worker_job_id, "status": status, "exit_code": exit_code,
                "work_dir": row["work_dir"], "command_id": row["command_id"],
                "job_id": row["job_id"], "attempt_id": row["attempt_id"]}

    def reap(self):
        with self._lock:
            items = list(self._processes.items())
        for worker_job_id, process in items:
            code = process.poll()
            if code is None:
                continue
            with self.connect() as conn:
                conn.execute("UPDATE jobs SET status=?,exit_code=? WHERE worker_job_id=?",
                             ("completed" if code == 0 else "failed", code, worker_job_id))
            with self._lock:
                self._processes.pop(worker_job_id, None)
