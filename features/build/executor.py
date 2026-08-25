from __future__ import annotations

import base64
import os
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from foundation.ssh_security import configure_strict_host_keys


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class BuildExecutionError(RuntimeError):
    pass


@dataclass
class PreparedCommand:
    command: str
    workspace: str
    artifact_patterns: list[str]
    init_commands: list[str]


def safe_id(value: str, fallback: str = "job") -> str:
    cleaned = _SAFE_ID_RE.sub("_", str(value or "").strip())[:120]
    return cleaned or fallback


def validate_workspace(workspace: str, workspace_root: str) -> str:
    workspace = str(workspace or "").strip()
    workspace_root = str(workspace_root or "").strip()
    if not workspace:
        raise BuildExecutionError("workspace is required")
    if not workspace.startswith("/"):
        workspace = f"{workspace_root.rstrip('/')}/{workspace.lstrip('/')}"
    normalized = posixpath.normpath(workspace)
    root = posixpath.normpath(workspace_root) if workspace_root else ""
    if root and normalized != root and not normalized.startswith(root.rstrip("/") + "/"):
        raise BuildExecutionError("workspace escapes server workspace_root")
    return normalized


def build_command_from_template(template: dict[str, Any], server: dict[str, Any], parameters: dict[str, Any]) -> PreparedCommand:
    schema = template.get("parameters_schema") if isinstance(template.get("parameters_schema"), dict) else {}
    resolved: dict[str, str] = {}
    for name, spec in schema.items():
        spec = spec if isinstance(spec, dict) else {}
        value = parameters.get(name, spec.get("default", ""))
        if value in (None, "") and spec.get("required"):
            raise BuildExecutionError(f"required parameter missing: {name}")
        value = str(value or "")
        validation = str(spec.get("validation") or "standard")
        # 默认所有参数 shell quote 后再渲染；只有模板作者显式声明
        # trusted_shell_fragment 的完整命令片段才允许裸插入 shell 元字符。
        if validation == "trusted_shell_fragment":
            # 完整命令片段必须仍然通过 pattern/choices 白名单约束。
            spec_pattern = spec.get("pattern")
            spec_choices = {str(item) for item in spec.get("choices") or []}
            if not (spec_pattern or spec_choices):
                raise BuildExecutionError(
                    f"trusted_shell_fragment requires pattern or choices: {name}"
                )
        elif validation == "integer":
            try:
                num = int(value)
            except ValueError as exc:
                raise BuildExecutionError(f"parameter must be an integer: {name}") from exc
            spec_min = spec.get("min")
            spec_max = spec.get("max")
            if spec_min is not None and num < int(spec_min):
                raise BuildExecutionError(f"parameter below minimum ({spec_min}): {name}")
            if spec_max is not None and num > int(spec_max):
                raise BuildExecutionError(f"parameter above maximum ({spec_max}): {name}")
            value = str(num)
        # validation == "none"/"standard"：值最终由 shlex.quote 包裹渲染，
        # 元字符被中和，无需字符集白名单。
        if spec.get("choices") and value and value not in {str(item) for item in spec.get("choices") or []}:
            raise BuildExecutionError(f"invalid choice for parameter: {name}")
        spec_pattern = spec.get("pattern")
        if spec_pattern and value and not re.match(str(spec_pattern), value):
            raise BuildExecutionError(f"invalid format for parameter: {name}")
        spec_max_length = int(spec.get("max_length") or 0)
        if spec_max_length and len(value) > spec_max_length:
            raise BuildExecutionError(f"parameter too long (max {spec_max_length}): {name}")
        if validation == "trusted_shell_fragment":
            resolved[name] = value
        elif name == "workspace" or spec.get("type") == "path":
            # 工作区参数在路径上下文中渲染，随后由 validate_workspace 归一化；
            # quote 会破坏路径拼接。
            resolved[name] = value
        else:
            # 其余参数 quote 后渲染，防止改变参数边界或注入 shell 元字符。
            resolved[name] = shlex.quote(value)

    def render(label: str, value: str) -> str:
        unknown = [name for name in _PLACEHOLDER_RE.findall(value) if name not in resolved]
        if unknown:
            raise BuildExecutionError(f"unknown {label} parameter: {unknown[0]}")
        try:
            return value.format(**resolved)
        except Exception as exc:
            raise BuildExecutionError(f"failed to render {label}: {exc}") from exc

    command_template = str(template.get("command") or "").strip()
    if not command_template:
        raise BuildExecutionError("template command is required")
    command = render("command", command_template)
    workspace = validate_workspace(render("workspace", str(template.get("workspace") or "")), str(server.get("workspace_root") or ""))
    init_commands = [render("init command", str(item)) for item in template.get("init_commands") or []]
    patterns = list(template.get("artifact_patterns") or server.get("artifact_patterns") or [])
    return PreparedCommand(
        command=command,
        workspace=workspace,
        artifact_patterns=[str(item) for item in patterns],
        init_commands=init_commands,
    )


class SshTmuxBuildBackend:
    def _connect_kwargs(self, server: dict[str, Any]) -> dict[str, Any]:
        auth = server.get("auth") if isinstance(server.get("auth"), dict) else {}
        kwargs: dict[str, Any] = {
            "hostname": server.get("host"),
            "port": int(server.get("port") or 22),
            "username": server.get("username"),
            "timeout": 20,
            "banner_timeout": 20,
            "auth_timeout": 20,
            "look_for_keys": True,
        }
        if auth.get("type") == "env_password":
            env_name = str(auth.get("env") or "").strip()
            if not env_name:
                raise BuildExecutionError("构建服务器未配置 SSH 密码环境变量")
            password = os.getenv(env_name)
            if not password:
                raise BuildExecutionError(f"构建服务器 SSH 密码未配置：请设置环境变量 {env_name}")
            kwargs["password"] = password
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
        elif auth.get("type") == "runtime_password":
            password = str(auth.get("password") or "")
            if not password:
                raise BuildExecutionError("本次构建未提供 SSH 密码")
            kwargs["password"] = password
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
        elif auth.get("type") == "password":
            password = str(auth.get("password") or "")
            if not password:
                raise BuildExecutionError("构建服务器 SSH 密码为空")
            kwargs["password"] = password
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
        elif auth.get("type") == "key":
            key_path = os.path.expanduser(str(auth.get("path") or "~/.ssh/id_rsa"))
            if key_path:
                kwargs["key_filename"] = key_path
        return kwargs

    def _run(self, server: dict[str, Any], command: str, timeout: int = 30) -> tuple[int, str, str]:
        import paramiko

        ssh = paramiko.SSHClient()
        configure_strict_host_keys(ssh)
        try:
            ssh.connect(**self._connect_kwargs(server))
            _stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout, get_pty=False)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            code = stdout.channel.recv_exit_status()
            return code, out, err
        except BuildExecutionError:
            raise
        except paramiko.AuthenticationException as exc:
            raise BuildExecutionError("连接构建服务器失败：SSH 用户名或密码错误") from exc
        except paramiko.PasswordRequiredException as exc:
            raise BuildExecutionError("连接构建服务器失败：SSH 私钥需要密码") from exc
        except (paramiko.SSHException, OSError, TimeoutError) as exc:
            raise BuildExecutionError(f"连接构建服务器失败：{exc}") from exc
        finally:
            ssh.close()

    def start(self, *, server: dict[str, Any], job_id: str, prepared: PreparedCommand, init_commands: list[str], timeout_sec: int) -> dict[str, str]:
        session = f"gms_build_{safe_id(job_id)}"
        log_dir = f"{prepared.workspace.rstrip('/')}/.gms_build_logs"
        log_path = f"{log_dir}/{safe_id(job_id)}.log"
        rc_path = f"{log_dir}/{safe_id(job_id)}.rc"
        done_path = f"{log_dir}/{safe_id(job_id)}.done"
        script_path = f"{log_dir}/{safe_id(job_id)}.sh"
        commands = [
            "#!/usr/bin/env bash",
            "set -o pipefail",
            f"cd {shlex.quote(prepared.workspace)}",
            *[str(cmd) for cmd in init_commands or []],
            prepared.command,
        ]
        script = "\n".join(commands) + "\n"
        script_b64 = base64.b64encode(script.encode()).decode()
        remote = (
            f"mkdir -p {shlex.quote(log_dir)} && "
            f"rm -f {shlex.quote(log_path)} {shlex.quote(rc_path)} {shlex.quote(done_path)} && "
            f"printf %s {shlex.quote(script_b64)} | base64 -d > {shlex.quote(script_path)} && "
            f"chmod +x {shlex.quote(script_path)} && "
            f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null || true; "
            f"tmux new-session -d -s {shlex.quote(session)} "
            f"\"bash -lc 'timeout {int(timeout_sec)}s {shlex.quote(script_path)} > {shlex.quote(log_path)} 2>&1; "
            f"echo \\$? > {shlex.quote(rc_path)}; date -Is > {shlex.quote(done_path)}'\""
        )
        code, out, err = self._run(server, remote, timeout=30)
        if code != 0:
            raise BuildExecutionError(err or out or "failed to start remote build")
        return {"session": session, "log_path": log_path, "rc_path": rc_path, "done_path": done_path}

    def poll(self, *, server: dict[str, Any], job: dict[str, Any], artifact_patterns: list[str]) -> dict[str, Any]:
        log_path = job.get("remote_log_path", "")
        log_dir = str(PurePosixPath(log_path).parent)
        rc_path = f"{log_dir}/{safe_id(job['id'])}.rc"
        done_path = f"{log_dir}/{safe_id(job['id'])}.done"
        command = (
            f"if test -f {shlex.quote(done_path)}; then "
            f"echo __DONE__; cat {shlex.quote(rc_path)} 2>/dev/null || echo 1; "
            f"else echo __RUNNING__; fi"
        )
        code, out, err = self._run(server, command, timeout=10)
        if code != 0:
            return {"status": "failed", "error": err or out or "failed to poll build"}
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if "__RUNNING__" in lines:
            return {"status": "running"}
        if "__DONE__" not in lines:
            return {"status": "running"}
        rc = 1
        for line in lines:
            if line.isdigit():
                rc = int(line)
                break
        artifacts = self.discover_artifacts(server=server, workspace=job.get("remote_workspace", ""), patterns=artifact_patterns)
        if rc == 0:
            return {"status": "completed", "artifacts": artifacts}
        return {"status": "failed", "error": f"build exited with code {rc}", "artifacts": artifacts}

    def tail_log(self, *, server: dict[str, Any], log_path: str, lines: int = 200) -> str:
        code, out, err = self._run(server, f"tail -n {int(lines)} {shlex.quote(log_path)} 2>/dev/null || true", timeout=10)
        return out if code == 0 else err

    def cancel(self, *, server: dict[str, Any], session: str) -> None:
        self._run(server, f"tmux send-keys -t {shlex.quote(session)} C-c 2>/dev/null || true; tmux kill-session -t {shlex.quote(session)} 2>/dev/null || true", timeout=10)

    def discover_artifacts(self, *, server: dict[str, Any], workspace: str, patterns: list[str]) -> list[dict[str, Any]]:
        if not patterns:
            return []
        find_parts = []
        for pattern in patterns[:20]:
            relative = PurePosixPath(str(pattern))
            parts = relative.parts
            static_parts: list[str] = []
            for part in parts:
                if any(char in part for char in "*?["):
                    break
                static_parts.append(part)
            if len(static_parts) == len(parts) and static_parts:
                static_parts.pop()
            search_root = PurePosixPath(workspace).joinpath(*static_parts)
            remaining_depth = max(1, len(parts) - len(static_parts))
            full_pattern = PurePosixPath(workspace) / relative
            find_parts.append(
                f"find {shlex.quote(str(search_root))} -maxdepth {remaining_depth} "
                f"-path {shlex.quote(str(full_pattern))} -type f "
                "-printf '%p\\t%s\\t%TY-%Tm-%TdT%TH:%TM:%TSZ\\n' 2>/dev/null || true"
            )
        command = " ; ".join(find_parts)
        code, out, _err = self._run(server, command, timeout=60)
        if code not in (0, 1):
            return []
        artifacts = []
        seen = set()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[0] in seen:
                continue
            seen.add(parts[0])
            artifacts.append({"path": parts[0], "size": int(parts[1] or 0), "mtime": parts[2]})
        artifacts.sort(key=lambda item: item.get("mtime", ""), reverse=True)
        return artifacts[:50]


class LocalBuildBackend(SshTmuxBuildBackend):
    def _run(self, server: dict[str, Any], command: str, timeout: int = 30) -> tuple[int, str, str]:
        # Build templates are operator-controlled shell programs; request
        # parameters are schema-validated before rendering.
        proc = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=timeout)  # nosec B602
        return proc.returncode, proc.stdout, proc.stderr
