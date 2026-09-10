#!/usr/bin/env python3
"""GMS Agent Runtime MCP launcher — Python edition (10.txt §十九/§十八).

Agent manifests launch the MCP server through this launcher instead of
python3 mcp_server.py directly. It loads the per-client profile WITHOUT any
shell involvement:

    ~/.config/gms-agent/profiles/<profile>.toml   (authoritative, 0600)
    ${XDG_DATA_HOME:-~/.local/share}/gms-remote-test/mcp/<client>.env  (legacy)

Selection order for the client name (first wins):
  1. $GMS_AGENT_CLIENT (set by the plugin manifest env block)
  2. first existing profile/env among kimi, codex, kkagent

Security: agents never hold platform passwords — the profile carries only
the Controller URL, CA path, token file path and the service-token auth
mode flag. GMS_AGENT_AUTH_MODE is forced to service-token: the MCP server
refuses to register password tools without it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CLIENTS = ("kimi", "codex", "kkagent")
MCP_ENV_DIR = Path(
    os.environ.get(
        "GMS_MCP_ENV_DIR",
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "gms-remote-test" / "mcp",
    )
)
PROFILE_ROOT = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "gms-agent" / "profiles"


def _apply_env(values: dict[str, str]) -> None:
    for key, value in values.items():
        if value:
            os.environ[key] = value


def _load_env_file(env_file: Path) -> None:
    """Legacy .env loader (safe: only exports, shlex-parsed values)."""
    import shlex

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        key, _, value = line[len("export "):].partition("=")
        parts = shlex.split(value.strip())
        if parts:
            os.environ[key.strip()] = parts[0]


def load_profile(client: str) -> bool:
    """Populate the environment for a client; returns True when found."""
    # 1. TOML profile (authoritative): profile files are named
    #    <client>-<host>-<uid>.toml.
    if PROFILE_ROOT.is_dir():
        import shlex  # noqa: F401  (keep parity with legacy loader context)

        for candidate in sorted(PROFILE_ROOT.glob(f"{client}-*.toml")):
            values = _read_toml_flat(candidate)
            if values:
                _apply_env(values)
                return True
    # 2. Legacy .env fallback.
    env_file = MCP_ENV_DIR / f"{client}.env"
    if env_file.is_file():
        _load_env_file(env_file)
        return True
    return False


def _read_toml_flat(path: Path) -> dict[str, str]:
    """Minimal TOML reader → flat {ENV_NAME: value} mapping."""
    section = ""
    data: dict[str, dict[str, str]] = {"": {}}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        data[section][key] = value
    top = data.get("", {})
    controller = data.get("controller", {})
    auth = data.get("auth", {})
    mapping: dict[str, str] = {
        "GMS_RT_PROFILE": top.get("profile", ""),
        "GMS_AGENT_CLIENT": top.get("client", ""),
        "GMS_REMOTE_TEST_SERVER": controller.get("url", ""),
        "GMS_CURL_CA_CERT": controller.get("ca_cert", ""),
        "GMS_AUTH_TOKEN_FILE": auth.get("token_file", ""),
    }
    return {k: v for k, v in mapping.items() if v}


def main() -> int:
    client = os.environ.get("GMS_AGENT_CLIENT", "")
    if not client:
        for candidate in CLIENTS:
            has_env = (MCP_ENV_DIR / f"{candidate}.env").is_file()
            has_toml = (
                bool(list(PROFILE_ROOT.glob(f"{candidate}-*.toml")))
                if PROFILE_ROOT.is_dir() else False
            )
            if has_env or has_toml:
                client = candidate
                break
    if client:
        load_profile(client)

    # Agents must never run in password mode even if the profile is missing:
    # the MCP server itself refuses to register password tools without this.
    os.environ.setdefault("GMS_AGENT_AUTH_MODE", "service-token")

    os.execv(
        sys.executable,
        [sys.executable, str(SCRIPT_DIR / "mcp_server.py"), *sys.argv[1:]],
    )


if __name__ == "__main__":
    raise SystemExit(main())
