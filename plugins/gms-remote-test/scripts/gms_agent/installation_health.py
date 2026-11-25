"""Secret-free Agent installation and client registration diagnostics."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from gms_agent import profile_store


def _controller_url_valid(value: str) -> bool:
    """Validate a Controller base URL without making a network request."""

    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname or ""
    return bool(
        parsed.scheme in {"http", "https"}
        and hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
        and re.fullmatch(r"[A-Za-z0-9.:-]+", hostname)
    )


def _token_file_status(value: str) -> dict[str, object]:
    """Return token-file metadata without reading or exposing the token."""

    path = Path(value).expanduser() if value else None
    present = bool(path and path.is_file())
    mode = stat.S_IMODE(path.stat().st_mode) if present and path else None
    owner_ok = bool(present and path and path.stat().st_uid == os.geteuid())
    return {
        "configured": bool(value),
        "present": present,
        "mode_ok": mode == 0o600,
        "owner_ok": owner_ok,
        "path": str(path) if path else "",
    }


_LAUNCHER_MARKER = "mcp_launcher.py"


def _launcher_from_args(args: list) -> str:
    """Return the mcp_launcher.py path inside an MCP args list ("")."""
    for item in args:
        text = str(item)
        if _LAUNCHER_MARKER in text:
            return text
    return ""


def _launcher_from_toml_block(text: str, marker: str) -> str:
    """Extract the mcp_launcher.py path from one [mcp_servers.*] block."""

    block = re.search(
        r"(?ms)^" + re.escape(marker) + r"\]\s*(?P<body>.*?)(?=^\[|\Z)", text
    )
    if block is None:
        return ""
    args_match = re.search(r"(?m)^args\s*=\s*\[(.*?)\]", block.group("body"), re.S)
    if args_match is None:
        return ""
    # TOML basic strings use "..."; literal strings use '...'. Only the
    # launcher path is extracted — no full TOML parse needed here.
    items = [
        value
        for pair in re.findall(r'"([^"]*)"|\'([^\']*)\'', args_match.group(1))
        for value in pair
        if value
    ]
    return _launcher_from_args(items)


def mcp_registration_status(client: str, client_skill_root: Callable[[str], Path]) -> dict[str, object]:
    """Inspect the client's GMS MCP registration marker and launchability.

    ``registered`` only says a registration block exists; ``launchable``
    says the referenced mcp_launcher.py payload is really on disk (#654649
    nightly 复盘：config.toml 注册块残留而 ~/.kkagent/plugins/local/
    gms-remote-test payload 被清空，kkagent 每个会话都起不来 gms MCP
    server，四条晨报分析全部 evidence_gate_failed）。doctor 必须两者都查，
    否则文本残留会误报 healthy。
    """

    launcher = ""
    if client == "kimi":
        path = client_skill_root(client).parent / "mcp.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            block = (payload.get("mcpServers") or {}).get("gms") or {}
            args = block.get("args") or []
            registered = any(_LAUNCHER_MARKER in str(item) for item in args)
            launcher = _launcher_from_args(args)
        except (OSError, ValueError, AttributeError):
            registered = False
    else:
        path = client_skill_root(client).parent / "config.toml"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        marker = (
            "[mcp_servers.gms]" if client == "kkagent"
            else "[mcp_servers.gms_remote_test]"
        )
        registered = marker in text and _LAUNCHER_MARKER in text
        launcher = _launcher_from_toml_block(text, marker.rstrip("]"))
        if client == "codex" and not registered:
            # Codex native plugins own their MCP registration through the
            # plugin manifest, so no standalone [mcp_servers.*] block is
            # expected. Recognize an enabled personal/team marketplace entry.
            plugin_block = re.search(
                r'(?ms)^\[plugins\."gms-remote-test@[^"\n]+"\]\s*'
                r"(?P<body>.*?)(?=^\[|\Z)",
                text,
            )
            registered = bool(
                plugin_block
                and re.search(
                    r"(?m)^enabled\s*=\s*true\s*$", plugin_block.group("body")
                )
            )
            # The launcher lives inside the plugin payload managed by the
            # host client; doctor cannot verify it from here.
            launcher = ""
    launchable = True
    if registered and launcher:
        launchable = Path(launcher).expanduser().is_file()
    return {
        "registered": registered,
        "config_path": str(path),
        "launchable": launchable,
        "launcher_path": launcher,
    }


def _host_environment_status() -> dict[str, object]:
    """Probe the filesystem basics the installed CLI depends on at runtime.

    The shell CLI writes its Bearer header through mktemp under TMPDIR, and
    full or read-only filesystems have historically surfaced as misleading
    downstream failures (e.g. "无法解析 Agent profile") instead of an
    obvious ENOSPC. Keep this secret-free and cheap: one probe file plus a
    statvfs sample.
    """

    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    state: dict[str, object] = {"tmpdir": str(tmpdir), "tmpdir_writable": False}
    try:
        probe = tmpdir / f".gms-agent-doctor-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        state["tmpdir_writable"] = True
    except OSError:
        pass
    try:
        usage = shutil.disk_usage(tmpdir)
        state["disk_free_gb"] = round(usage.free / 1024**3, 2)
        state["disk_total_gb"] = round(usage.total / 1024**3, 2)
    except OSError:
        pass
    return state


def build_doctor_report(
    clients: list[str], profile: str, *, installed: str, running: str,
    current_cli: str, runtime_root: Path, versions_dir: Path,
    client_skill_root: Callable[[str], Path],
    registration_status: Callable[[str], dict[str, object]],
    host_environment_status: Callable[[], dict[str, object]],
    running_runtimes: Callable[[Path], list[dict]],
) -> dict[str, object]:
    """Inspect profile, token, MCP registration and runtime consistency."""
    report_clients: list[dict[str, object]] = []
    actions: list[str] = []

    for client_name in clients:
        candidates = profile_store.profile_candidates(client_name)
        selected = None
        if profile:
            candidate = profile_store.profile_path(profile)
            if candidate in candidates:
                selected = candidate
        elif len(candidates) == 1:
            selected = candidates[0]

        profile_state: dict[str, object] = {
            "count": len(candidates),
            "names": [path.stem for path in candidates],
            "selected": selected.stem if selected else "",
            "valid": False,
        }
        token_state = _token_file_status("")
        tls_verify_disabled = False
        if selected:
            flat = profile_store.load_profile(selected.stem)
            server = flat.get("url", "")
            ca_cert = flat.get("ca_cert", "")
            # 安装期 GMS_INSTALL_INSECURE=1 会被持久化为 insecure=true 并
            # 放大为运行期常态（launcher 映射 GMS_CURL_INSECURE=1）。doctor
            # 把该状态作为醒目字段输出，生产部署清单可据此拒绝。
            tls_verify_disabled = flat.get("insecure", "") == "true"
            token_state = _token_file_status(flat.get("token_file", ""))
            controller_valid = _controller_url_valid(server)
            profile_state.update(
                {
                    "controller": server,
                    "controller_url_valid": controller_valid,
                    "ca_configured": bool(ca_cert),
                    "ca_present": bool(ca_cert and Path(ca_cert).is_file()),
                    "tls_verify_disabled": tls_verify_disabled,
                    "valid": bool(controller_valid),
                }
            )
            if not controller_valid:
                actions.append(
                    f"replace the invalid Controller URL in profile {selected.stem}"
                )
            if ca_cert and not Path(ca_cert).is_file():
                actions.append(f"restore the Controller CA file for {client_name}")
            if tls_verify_disabled:
                actions.append(
                    f"profile {selected.stem} runs with TLS verification "
                    "disabled (insecure=true): configure GMS_CURL_CA_CERT and "
                    "re-run gms-agent install — throwaway environments only"
                )
        elif len(candidates) > 1:
            actions.append(
                f"select one {client_name} profile explicitly with GMS_RT_PROFILE"
            )
        else:
            actions.append(f"install/configure the {client_name} profile")

        if not token_state["present"]:
            actions.append(f"enroll an Agent Service Token for {client_name}")
        elif not token_state["mode_ok"] or not token_state["owner_ok"]:
            actions.append(f"fix Agent Service Token ownership/mode for {client_name}")

        mcp_state = registration_status(client_name)
        if not mcp_state["registered"]:
            actions.append(f"reconcile the {client_name} MCP registration")
        elif not mcp_state["launchable"]:
            actions.append(
                f"restore the installed runtime and reconcile the {client_name} "
                "MCP registration — the registered launcher is missing: "
                f"{mcp_state['launcher_path']} (run python "
                "tools/scripts/agent/sync_package.py . in the repo, then "
                f"gms-agent install --client {client_name})"
            )
        skill_path = client_skill_root(client_name) / "gms-remote-test"
        if not (skill_path / "SKILL.md").is_file():
            actions.append(f"install the {client_name} Skill payload")
        report_clients.append(
            {
                "client": client_name,
                "skill_present": (skill_path / "SKILL.md").is_file(),
                "skill_path": str(skill_path),
                "profile": profile_state,
                "token": token_state,
                "mcp": mcp_state,
            }
        )

    host_env = host_environment_status()
    if not host_env.get("tmpdir_writable"):
        actions.append(
            f"free space or fix permissions for TMPDIR ({host_env['tmpdir']}) — "
            "the shell CLI needs it for Bearer header temp files"
        )

    versions_consistent = bool(installed and installed == current_cli == running)
    if not versions_consistent:
        actions.append("install/activate one complete package version")
    processes = running_runtimes(versions_dir)
    warnings = [
        f"MCP PID {item['pid']} still runs {item['version']}; the resident server follows "
        f"`current` on its next idle request (version_guard), or restart the client to "
        f"load {installed} right now"
        for item in processes if item["version"] != installed
    ]
    unique_actions = list(dict.fromkeys(actions))
    installation_healthy = versions_consistent and bool(host_env.get("tmpdir_writable"))
    registered_clients_healthy = (
        all(
            bool(item["skill_present"])
            and bool(item["profile"]["valid"])
            and bool(item["token"]["present"])
            and bool(item["token"]["mode_ok"])
            and bool(item["token"]["owner_ok"])
            and bool(item["mcp"]["registered"])
            and item["mcp"].get("launchable") is True
            for item in report_clients
        )
    )
    return {
        "ok": installation_healthy and registered_clients_healthy,
        "installation_healthy": installation_healthy,
        "registered_clients_healthy": registered_clients_healthy,
        "all_mcp_sessions_current": not warnings,
        "reconnect_required": bool(warnings),
        "versions": {
            "running": running,
            "installed": installed or None,
            "current_cli": current_cli or None,
            "consistent": versions_consistent,
        },
        "runtime_root": str(runtime_root),
        "host_environment": host_env,
        "clients": report_clients,
        "actions": unique_actions,
        "mcp_processes": processes,
        "warnings": warnings,
    }


def running_runtimes(versions: Path, proc: Path = Path("/proc")) -> list[dict]:
    """Find exact versioned launcher/server argv paths, including deleted ones."""
    try:
        processes = list(proc.iterdir())
    except OSError:
        return []
    root = versions.resolve()
    result = []
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if len(argv) < 2:
                continue
            # The launcher execs [python, absolute-script, ...]. Never search
            # arbitrary arguments or command strings for a version substring.
            script = Path(os.fsdecode(argv[1]))
            relative = script.relative_to(root)
        except (OSError, ValueError):
            continue
        if (len(relative.parts) == 3 and relative.parts[1] == "scripts"
                and relative.name in {"mcp_server.py", "mcp_launcher.py"}):
            result.append({"pid": int(process.name), "version": relative.parts[0]})
    return sorted(result, key=lambda item: item["pid"])
