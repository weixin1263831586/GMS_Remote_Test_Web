#!/usr/bin/env python3
"""Desired-state reconciler for agent MCP registrations (10.txt §十五).

Historically the installer treated an existing registration as done: if
``mcp_servers.gms`` was present it skipped, so a migrated Controller URL, a
rotated CA bundle or a new token path kept serving the OLD configuration
forever. This module is a real reconciler:

  * parse the existing client config,
  * if invalid → FAIL (back up the file first, never silently overwrite),
  * if a gms block exists → compare it to the desired state and update only
    the gms block (user's other MCP servers stay untouched),
  * if absent → insert it.

Used by scripts/install.sh and by the `gms-agent` installer CLI.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any


DESIRED_ENV_KEYS = (
    "GMS_REMOTE_TEST_SERVER",
    "GMS_RT_PROFILE",
    "GMS_AUTH_TOKEN_FILE",
    "GMS_AGENT_AUTH_MODE",
    "GMS_CURL_CA_CERT",
)


class ReconcileError(RuntimeError):
    """The client config exists but cannot be parsed — never overwrite it."""


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def _desired_env(server_url: str, profile: str, token_file: str, ca_cert: str) -> dict[str, str]:
    env = {
        "GMS_REMOTE_TEST_SERVER": server_url,
        "GMS_RT_PROFILE": profile,
        "GMS_AUTH_TOKEN_FILE": token_file,
        # 11.txt 审核 P0-3：注册进客户端配置的 MCP 环境必须显式声明
        # service-token 模式——否则 mcp_server.py 会注册密码登录/提权/
        # 自助审批工具，重新打开 10.txt 指出的高危边界。
        "GMS_AGENT_AUTH_MODE": "service-token",
    }
    if ca_cert:
        env["GMS_CURL_CA_CERT"] = ca_cert
    return env


def _desired_block(mcp_server_path: str, env: dict[str, str]) -> dict[str, Any]:
    return {"command": "python3", "args": [mcp_server_path], "env": env}


def reconcile_kimi(
    config_path: str | Path,
    mcp_server_path: str,
    server_url: str,
    profile: str,
    token_file: str,
    ca_cert: str = "",
) -> str:
    """Reconcile ~/.kimi-code/mcp.json. Returns a human-readable action."""
    path = Path(config_path).expanduser()
    env = _desired_env(server_url, profile, token_file, ca_cert)
    desired = _desired_block(mcp_server_path, env)

    config: dict[str, Any] = {}
    existed = path.exists()
    if existed:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("top level is not an object")
            config = parsed
        except (ValueError, OSError) as error:
            # 10.txt §十四: a single comma error must never let the installer
            # rewrite the user's whole mcp.json down to only-GMS.
            backup = _backup(path)
            raise ReconcileError(
                f"{path} 无法解析（{error}）。已备份到 {backup}；"
                "请修复或删除该文件后重新安装。安装器拒绝覆盖损坏的用户配置。"
            ) from error

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ReconcileError(f"{path} 的 mcpServers 不是对象；拒绝修改。")

    action = "registered"
    if "gms" in servers:
        if servers["gms"] == desired:
            return f"unchanged: {path}"
        action = "updated"
    servers["gms"] = desired

    path.parent.mkdir(parents=True, exist_ok=True)
    if existed:
        _backup(path)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"{action}: {path}"


def reconcile_codex(
    config_path: str | Path,
    mcp_server_path: str,
    server_url: str,
    profile: str,
    token_file: str,
    ca_cert: str = "",
) -> str:
    """Reconcile the [mcp_servers.gms_remote_test] table of Codex config.toml.

    TOML is edited with a targeted block replace: everything between the
    ``[mcp_servers.gms_remote_test]`` header and the next ``[`` header (or
    EOF) is regenerated from the desired state; the rest of the file is
    preserved byte-for-byte.
    """
    path = Path(config_path).expanduser()
    env = _desired_env(server_url, profile, token_file, ca_cert)

    def toml_str(value: str) -> str:
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\t", "\\t")
            .replace("\n", "\\n")
        )
        return f'"{escaped}"'

    lines = [
        "[mcp_servers.gms_remote_test]",
        'command = "python3"',
        f"args = [{toml_str(mcp_server_path)}]",
        "",
        "[mcp_servers.gms_remote_test.env]",
    ]
    for key in DESIRED_ENV_KEYS:
        if key in env:
            lines.append(f"{key} = {toml_str(env[key])}")
    desired_block = "\n".join(lines) + "\n"

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    marker = "[mcp_servers.gms_remote_test]"
    if marker in text:
        start = text.index(marker)
        # The block runs to the next TOML section header (a line starting
        # with '[') or EOF. Scan line-wise so nested "[mcp_servers...env]"
        # lines inside the block are not mistaken for the boundary — only a
        # header that is NOT part of this gms table ends the block.
        next_section = len(text)
        for match in re.finditer(r"^(\[.+)\]$", text[start + 1:], flags=re.M):
            header = match.group(1)
            if header.startswith("[mcp_servers.gms_remote_test"):
                continue
            next_section = start + 1 + match.start()
            break
        existing_block = text[start:next_section]
        if existing_block.strip() + "\n" == desired_block:
            return f"unchanged: {path}"
        new_text = text[:start] + desired_block + text[next_section:]
        action = "updated"
    else:
        new_text = (text + ("\n" if text and not text.endswith("\n") else "")) + "\n" + desired_block
        action = "registered"

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _backup(path)
    path.write_text(new_text, encoding="utf-8")
    return f"{action}: {path}"


def main() -> int:
    # Thin CLI wrapper: agent_mcp_config.py codex|kimi <config> <mcp_server>
    #                    <server_url> <profile> <token_file> [ca_cert]
    if len(sys.argv) < 8:
        print(
            "usage: agent_mcp_config.py codex|kimi CONFIG MCP_SERVER SERVER_URL "
            "PROFILE TOKEN_FILE [CA_CERT]",
            file=sys.stderr,
        )
        return 2
    client, config, mcp_server, server_url, profile, token_file = sys.argv[1:7]
    ca_cert = sys.argv[7] if len(sys.argv) > 7 else ""
    try:
        if client == "kimi":
            print(reconcile_kimi(config, mcp_server, server_url, profile, token_file, ca_cert))
        elif client == "codex":
            print(reconcile_codex(config, mcp_server, server_url, profile, token_file, ca_cert))
        else:
            print(f"unknown client: {client}", file=sys.stderr)
            return 2
    except ReconcileError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
