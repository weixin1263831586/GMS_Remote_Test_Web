from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
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
                error TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                worker_job_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                pid INTEGER, pgid INTEGER, status TEXT NOT NULL, devices_json TEXT NOT NULL,
                work_dir TEXT NOT NULL, exit_code INTEGER, error TEXT NOT NULL,
                command_id TEXT NOT NULL DEFAULT '')""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "command_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN command_id TEXT NOT NULL DEFAULT ''")

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
                updated_at=CURRENT_TIMESTAMP""",
                (command_id, status, json.dumps(result or {}, separators=(",", ":")), error))

    def running_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        return [{"worker_job_id": row["worker_job_id"], "job_id": row["job_id"],
                 "attempt_id": row["attempt_id"], "pid": row["pid"],
                 "status": row["status"], "devices": json.loads(row["devices_json"])} for row in rows]

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
        worker_job_id = payload.get("worker_job_id") or f"wj-{command['id']}"
        work_dir = self.config.data_root / "jobs" / (command.get("job_id") or command["id"]) / (command.get("attempt_id") or "1")
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout_file = open(work_dir / "stdout.log", "ab", buffering=0)
        stderr_file = open(work_dir / "stderr.log", "ab", buffering=0)
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (payload.get("env") or {}).items()})
        exit_code_path = work_dir / "exit_code"
        wrapper = '"$@"; rc=$?; printf "%s" "$rc" > "$GMS_EXIT_CODE_FILE"; exit "$rc"'
        env["GMS_EXIT_CODE_FILE"] = str(exit_code_path)
        process = subprocess.Popen(["/bin/bash", "-c", wrapper, "gms-worker-job", *argv],
                                   cwd=payload.get("cwd") or str(executable.parent),
                                   env=env, stdout=stdout_file, stderr=stderr_file,
                                   start_new_session=True)
        with self._lock:
            self._processes[worker_job_id] = process
        with self.connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO jobs
                (worker_job_id,job_id,attempt_id,pid,pgid,status,devices_json,
                 work_dir,exit_code,error,command_id) VALUES(?,?,?,?,?,'running',?,?,NULL,'',?)""",
                (worker_job_id, command.get("job_id", ""), command.get("attempt_id", ""),
                 process.pid, os.getpgid(process.pid), json.dumps(payload.get("devices", [])),
                 str(work_dir), command["id"]))
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
