from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from features.build.config import BuildConfigRepository
from features.build.executor import (
    BuildExecutionError,
    LocalBuildBackend,
    SshTmuxBuildBackend,
    build_command_from_template,
    validate_workspace,
)
from features.build.models import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    TERMINAL_JOB_STATUSES,
    BuildJobCreateRequest,
    utc_now_iso,
)
from features.build.repository import BuildStore


logger = logging.getLogger(__name__)


class BuildNotFoundError(LookupError):
    pass


class BuildService:
    def __init__(self, *, store: BuildStore, config_path: str | Path):
        self.store = store
        self.config = BuildConfigRepository(config_path)
        self.backends = {
            "ssh": SshTmuxBuildBackend(),
            "local": LocalBuildBackend(),
        }
        self._runtime_passwords: dict[str, str] = {}

    @staticmethod
    def new_job_id() -> str:
        return f"build_{uuid.uuid4().hex[:12]}"

    def list_servers(self) -> list[dict[str, Any]]:
        return [self._public_server(item) for item in self.config.list_servers()]

    def list_templates(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return self.config.list_templates(enabled_only=enabled_only)

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise BuildNotFoundError("Build job not found")
        return self._decorate_job(job)

    def list_jobs(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return [self._decorate_job(item) for item in self.store.list_jobs(status=status, limit=limit)]

    def delete_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            raise BuildNotFoundError("Build job not found")
        if job["status"] not in TERMINAL_JOB_STATUSES:
            raise BuildExecutionError("只能删除已完成、失败或已取消的历史构建任务")
        if not self.store.delete_job(job_id):
            raise BuildNotFoundError("Build job not found")
        self._runtime_passwords.pop(job_id, None)

    def create_job(self, request: dict[str, Any], *, start: bool = True) -> dict[str, Any]:
        req = BuildJobCreateRequest(**(request or {}))
        server = self._with_runtime_password(self._get_server(req.server_id), req.server_password)
        template = self._get_template(req.template_id)
        if template.get("server_id") and template["server_id"] != server["id"]:
            raise BuildExecutionError("template is not allowed on selected server")

        # Concurrency control: respect server.max_concurrent_jobs. Only servers
        # using non-password auth (SSH key / env_password from environment) are
        # allowed to queue, because the runtime password is keyed by job id and
        # is lost on restart — a queued-then-restarted job could never start.
        start = self._maybe_defer_for_capacity(server, start)

        prepared = build_command_from_template(template, server, req.parameters)
        now = utc_now_iso()
        job = self.store.create_job({
            "id": self.new_job_id(),
            "server_id": server["id"],
            "template_id": template["id"],
            "source_type": req.source_type,
            "source_key": req.source_key,
            "owner": req.owner,
            "automation_run_id": req.automation_run_id,
            "status": JOB_QUEUED,
            "remote_session": "",
            "remote_workspace": prepared.workspace,
            "remote_log_path": "",
            "command": prepared.command,
            "parameters_json": json.dumps(req.parameters, ensure_ascii=False, separators=(",", ":")),
            "artifact_json": "[]",
            "error": "",
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "finished_at": "",
        })
        if req.server_password:
            self._runtime_passwords[job["id"]] = req.server_password
        if start:
            job = self.start_job(job["id"], server_password=req.server_password)
        return self._decorate_job(job)

    def _maybe_defer_for_capacity(self, server: dict[str, Any], start: bool) -> bool:
        """Decide whether a new job can start now or must queue.

        Queuing is only allowed for servers whose auth is not a per-request
        runtime password (queued jobs survive restart only when the server can
        be reached without a job-bound secret). Returns the effective `start`.
        """
        if not start:
            return False
        max_concurrent = int(server.get("max_concurrent_jobs") or 1)
        if max_concurrent <= 0:
            return True
        in_flight = [
            job
            for job in self.store.list_jobs(status=JOB_RUNNING, limit=500)
            if job.get("server_id") == server["id"]
        ]
        if len(in_flight) < max_concurrent:
            return True
        # At capacity. Only queue if the server does not need a runtime password.
        auth = server.get("auth") if isinstance(server.get("auth"), dict) else {}
        queueable = auth.get("type") in {"key", "env_password", ""}
        if queueable:
            return False  # leave as JOB_QUEUED; worker promotes it when capacity frees
        raise BuildExecutionError(
            f"build server {server['id']} is at capacity ({max_concurrent}) and "
            "password-auth servers cannot queue; retry later"
        )

    def start_queued_jobs(self) -> int:
        """Promote queued jobs whose server now has free capacity. Returns count started."""
        started = 0
        for job in self.store.list_jobs(status=JOB_QUEUED, limit=100):
            server = self._with_runtime_password(
                self._get_server(job["server_id"]),
                self._runtime_passwords.get(job["id"], ""),
            )
            try:
                if not self._maybe_defer_for_capacity(server, True):
                    continue
                self.start_job(job["id"], server_password=self._runtime_passwords.get(job["id"], ""))
                started += 1
            except BuildExecutionError as exc:
                logger.info("Queued build %s remains deferred: %s", job.get("id"), exc)
                continue
            except Exception:
                logger.exception("Failed to start queued build %s", job.get("id"))
        return started

    def start_job(self, job_id: str, server_password: str = "") -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise BuildNotFoundError("Build job not found")
        if job["status"] != JOB_QUEUED:
            return job
        effective_password = server_password or self._runtime_passwords.get(job["id"], "")
        server = self._with_runtime_password(self._get_server(job["server_id"]), effective_password)
        template = self._get_template(job["template_id"])
        prepared = build_command_from_template(
            template,
            server,
            json.loads(job.get("parameters_json") or "{}"),
        )
        backend = self._backend(server)
        claimed = self.store.claim_queued_job(
            job["id"],
            server_id=server["id"],
            max_concurrent=int(server.get("max_concurrent_jobs") or 1),
        )
        if claimed is None:
            # A separate worker won the race. Return authoritative state and
            # never invoke the remote backend twice for the same job.
            return self.store.get_job(job["id"])
        job = claimed
        try:
            started = backend.start(
                server=server,
                job_id=job["id"],
                prepared=prepared,
                init_commands=prepared.init_commands,
                timeout_sec=int(template.get("timeout_sec") or 21600),
            )
        except Exception as exc:
            failed = self.store.update_job(
                job["id"],
                status=JOB_FAILED,
                error=str(exc),
                finished_at=utc_now_iso(),
            )
            self._runtime_passwords.pop(job["id"], None)
            return failed
        return self.store.update_job(
            job["id"],
            remote_session=started["session"],
            remote_log_path=started["log_path"],
        )

    def poll_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise BuildNotFoundError("Build job not found")
        if job["status"] in TERMINAL_JOB_STATUSES:
            self._runtime_passwords.pop(job_id, None)
            return self._decorate_job(job)
        if job["status"] == JOB_QUEUED:
            return self._decorate_job(self.start_job(job_id))
        server = self._with_runtime_password(self._get_server(job["server_id"]), self._runtime_passwords.get(job_id, ""))
        template = self._get_template(job["template_id"])
        backend = self._backend(server)
        result = backend.poll(
            server=server,
            job=job,
            artifact_patterns=list(template.get("artifact_patterns") or server.get("artifact_patterns") or []),
        )
        status = result.get("status")
        if status == JOB_RUNNING:
            return self._decorate_job(job)
        updates: dict[str, Any] = {}
        if status == JOB_COMPLETED:
            updates = {
                "status": JOB_COMPLETED,
                "artifact_json": json.dumps(result.get("artifacts") or [], ensure_ascii=False, separators=(",", ":")),
                "finished_at": utc_now_iso(),
            }
        elif status == JOB_FAILED:
            updates = {
                "status": JOB_FAILED,
                "error": result.get("error", "build failed"),
                "artifact_json": json.dumps(result.get("artifacts") or [], ensure_ascii=False, separators=(",", ":")),
                "finished_at": utc_now_iso(),
            }
        if updates:
            job = self.store.update_job(job_id, **updates)
            self._runtime_passwords.pop(job_id, None)
        return self._decorate_job(job)

    def tail_log(self, job_id: str, lines: int = 200) -> str:
        job = self.store.get_job(job_id)
        if not job:
            raise BuildNotFoundError("Build job not found")
        if not job.get("remote_log_path"):
            return ""
        server = self._with_runtime_password(self._get_server(job["server_id"]), self._runtime_passwords.get(job_id, ""))
        return self._backend(server).tail_log(server=server, log_path=job["remote_log_path"], lines=lines)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise BuildNotFoundError("Build job not found")
        if job["status"] in TERMINAL_JOB_STATUSES:
            self._runtime_passwords.pop(job_id, None)
            return self._decorate_job(job)
        server = self._with_runtime_password(self._get_server(job["server_id"]), self._runtime_passwords.get(job_id, ""))
        if job.get("remote_session"):
            self._backend(server).cancel(server=server, session=job["remote_session"])
        cancelled = self.store.update_job(
            job_id,
            status=JOB_CANCELLED,
            finished_at=utc_now_iso(),
        )
        self._runtime_passwords.pop(job_id, None)
        return self._decorate_job(cancelled)

    def set_job_password(self, job_id: str, server_password: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise BuildNotFoundError("Build job not found")
        if server_password and job["status"] not in TERMINAL_JOB_STATUSES:
            self._runtime_passwords[job_id] = server_password
        return self._decorate_job(job)

    def discover_workspaces(self, server_id: str, server_password: str = "", base_dir: str = "") -> list[str]:
        server = self._with_runtime_password(self._get_server(server_id), server_password)
        root = base_dir.strip() or str(server.get("workspace_root") or "")
        backend = self._backend(server)
        command = (
            f"find {root!r} -maxdepth 1 -mindepth 1 -type d "
            r"\( -name '*Android*' -o -name '*android*' \) -printf '%f\n' "
            "| sort"
        )
        code, out, err = backend._run(server, command, timeout=30)
        if code != 0:
            raise BuildExecutionError(err or out or "failed to discover SDK directories")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def discover_lunch_options(
        self,
        server_id: str,
        workspace: str,
        server_password: str = "",
    ) -> list[str]:
        server = self._with_runtime_password(self._get_server(server_id), server_password)
        workspace = validate_workspace(workspace, str(server.get("workspace_root") or ""))
        backend = self._backend(server)
        # 不调用交互式 rkbuild_lunch：无 stdin 时它可能选择默认项并把完整
        # lunch banner/环境信息输出到错误提示。直接读取当前源码树由 envsetup
        # 计算出的 COMMON_LUNCH_CHOICES，并用哨兵隔离 shell/profile 噪声。
        discover_command = (
            f"cd {workspace!r} && "
            "timeout 60s bash --noprofile --norc -c '"
            "unset TARGET_PRODUCT TARGET_RELEASE TARGET_BUILD_VARIANT TARGET_BUILD_APPS; "
            "if [ -f build/envsetup.sh ]; then source build/envsetup.sh >/dev/null 2>&1; "
            "elif [ -f build/make/envsetup.sh ]; then source build/make/envsetup.sh >/dev/null 2>&1; "
            "else exit 3; fi; "
            "printf \"__GMS_LUNCH_BEGIN__\\n\"; "
            "if command -v get_build_var >/dev/null 2>&1; then "
            "get_build_var COMMON_LUNCH_CHOICES 2>/dev/null | tr \" \" \"\\n\"; fi; "
            "printf \"__GMS_LUNCH_END__\\n\""
            "'"
        )
        code, out, err = backend._run(server, discover_command, timeout=75)
        if code != 0:
            detail = (err or "").strip().splitlines()[-1:] or [""]
            raise BuildExecutionError(f"读取 {workspace} 的 lunch 选项失败{': ' + detail[0][:240] if detail[0] else ''}")
        options = self._parse_scoped_lunch_options(out)
        if options:
            return options

        fallback_command = (
            f"cd {workspace!r} && "
            "bash --noprofile --norc -c '"
            "if [ -f build/envsetup.sh ]; then source build/envsetup.sh >/dev/null 2>&1; "
            "elif [ -f build/make/envsetup.sh ]; then source build/make/envsetup.sh >/dev/null 2>&1; fi; "
            "if command -v get_build_var >/dev/null 2>&1; then "
            "TARGET_BUILD_APPS= TARGET_PRODUCT= TARGET_RELEASE= TARGET_BUILD_VARIANT= get_build_var COMMON_LUNCH_CHOICES 2>/dev/null | tr \" \" \"\\n\"; "
            "fi; "
            "grep -R --include=\"AndroidProducts.mk\" -hE \"^[[:space:]]*[A-Za-z0-9_.-]+-(userdebug|user|eng)[[:space:]]*(\\\\\\\\)?$\" device vendor 2>/dev/null || true"
            "'"
        )
        code, fallback_out, fallback_err = backend._run(server, fallback_command, timeout=120)
        if code != 0:
            raise BuildExecutionError(fallback_err or fallback_out or "failed to discover lunch options")
        options = self._parse_lunch_options(fallback_out)
        if not options:
            logger.warning("No lunch options discovered for workspace %s", workspace)
            raise BuildExecutionError(f"源码目录 {workspace} 未发现可用 lunch 选项，请检查 envsetup 和 AndroidProducts.mk")
        return options

    @classmethod
    def _parse_scoped_lunch_options(cls, output: str) -> list[str]:
        match = re.search(
            r"^__GMS_LUNCH_BEGIN__\s*$([\s\S]*?)^__GMS_LUNCH_END__\s*$",
            output or "",
            flags=re.MULTILINE,
        )
        return cls._parse_lunch_options(match.group(1)) if match else []

    @staticmethod
    def _parse_lunch_options(output: str) -> list[str]:
        options: list[str] = []
        seen: set[str] = set()
        patterns = [
            re.compile(r"^\s*\d+[\).]\s*([A-Za-z0-9_.-]+-[A-Za-z0-9_.-]+)\s*$"),
            re.compile(r"^\s*[-*]\s*([A-Za-z0-9_.-]+-[A-Za-z0-9_.-]+)\s*$"),
            re.compile(r"\b([A-Za-z0-9_.-]+-(?:user|userdebug|eng))\b"),
        ]
        lines = (output or "").splitlines()
        if any("Lunch menu" in line for line in lines):
            scoped: list[str] = []
            in_menu = False
            for line in lines:
                if "Lunch menu" in line:
                    in_menu = True
                    continue
                if in_menu and "Which would you like" in line:
                    break
                if in_menu:
                    scoped.append(line)
            lines = scoped

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            for pattern in patterns:
                match = pattern.search(line)
                if not match:
                    continue
                value = match.group(1).strip()
                if not re.search(r"-(?:userdebug|user|eng)$", value):
                    continue
                if value and value not in seen:
                    seen.add(value)
                    options.append(value)
                break
        return options[:200]

    def _get_server(self, server_id: str) -> dict[str, Any]:
        server = self.config.get_server(server_id)
        if not server:
            raise BuildNotFoundError(f"Build server not found: {server_id}")
        return server

    def _get_template(self, template_id: str) -> dict[str, Any]:
        template = self.config.get_template(template_id)
        if not template:
            raise BuildNotFoundError(f"Build template not found: {template_id}")
        if not template.get("enabled", True):
            raise BuildExecutionError("Build template is disabled")
        return template

    def _backend(self, server: dict[str, Any]):
        return self.backends.get(str(server.get("backend") or "ssh")) or self.backends["ssh"]

    @staticmethod
    def _with_runtime_password(server: dict[str, Any], password: str = "") -> dict[str, Any]:
        if not password:
            return server
        updated = dict(server)
        updated["auth"] = {"type": "runtime_password", "password": password}
        return updated

    @staticmethod
    def _public_server(server: dict[str, Any]) -> dict[str, Any]:
        public = dict(server)
        if isinstance(public.get("auth"), dict):
            public["auth"] = {"type": public["auth"].get("type", "")}
        return public

    @staticmethod
    def _decorate_job(job: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(job)
        try:
            decorated["parameters"] = json.loads(job.get("parameters_json") or "{}")
        except json.JSONDecodeError:
            decorated["parameters"] = {}
        try:
            decorated["artifacts"] = json.loads(job.get("artifact_json") or "[]")
        except json.JSONDecodeError:
            decorated["artifacts"] = []
        return decorated
