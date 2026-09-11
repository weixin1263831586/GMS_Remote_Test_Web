#!/usr/bin/env python3
"""GMS Agent Runtime MCP launcher — Python edition.

Agent manifests launch the MCP server through this launcher instead of
python3 mcp_server.py directly. It loads the per-client profile WITHOUT any
shell involvement from the single authoritative source:

    ~/.config/gms-agent/profiles/<profile>.toml   (authoritative, 0600)

12.txt P1 (profile single source of truth): the legacy
``~/.local/share/gms-remote-test/mcp/<client>.env`` fallback and the
"first existing profile/env among kimi, codex, kkagent" probe were removed.
With codex-A → Controller A / codex-B → Controller B, a filesystem-glob
first-match is not an Agent routing policy — it silently picked the wrong
Controller. The client/profile must now be declared by the caller (MCP
registration env block or GMS_AGENT_PROFILE); an undeclared launch fails
with actionable guidance instead of guessing.

Selection order:
  1. $GMS_AGENT_CLIENT (set by the plugin manifest env block)
  2. $GMS_RT_PROFILE's own ``client =`` field
  3. $GMS_AGENT_PROFILE → <name>.toml

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
PROFILE_ROOT = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "gms-agent" / "profiles"


def _apply_env(values: dict[str, str]) -> None:
    for key, value in values.items():
        if value:
            os.environ[key] = value


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
    if controller.get("insecure", "").lower() == "true":
        mapping["GMS_CURL_INSECURE"] = "1"
    return {k: v for k, v in mapping.items() if v}


def load_profile(client: str) -> bool:
    """Populate the environment for a client; returns True when found."""
    if not PROFILE_ROOT.is_dir():
        return False
    for candidate in sorted(PROFILE_ROOT.glob(f"{client}-*.toml")):
        try:
            values = _read_toml_flat(candidate)
        except OSError as exc:
            # Unreadable/corrupt profile must not crash the launcher — skip
            # and let the next candidate (or the unconfigured server) win.
            print(
                f"mcp_launcher: skipping unreadable profile {candidate.name}: {exc}",
                file=sys.stderr,
            )
            continue
        if values:
            _apply_env(values)
            return True
    return False


def _client_from_profile(profile: str) -> str:
    """Read the ``client =`` field out of <profile>.toml (fail closed)."""
    profile_path = PROFILE_ROOT / f"{profile}.toml"
    if not profile_path.is_file():
        return ""
    try:
        declared = _read_toml_flat(profile_path).get("GMS_AGENT_CLIENT", "")
        return declared if declared in CLIENTS else ""
    except OSError:
        return ""


def main() -> int:
    client = os.environ.get("GMS_AGENT_CLIENT", "")
    profile = os.environ.get("GMS_RT_PROFILE", "")
    # 4.txt 审核 P1-5: with multiple clients configured, honoring the pinned
    # profile's own `client =` field avoids loading another client's
    # Controller URL and identity.
    if not client and profile:
        client = _client_from_profile(profile)
    # 12.txt P1: GMS_AGENT_PROFILE=<name> pins the profile file directly;
    # the historical first-match probe over kimi/codex/kkagent was removed.
    if not client and not profile:
        named_profile = os.environ.get("GMS_AGENT_PROFILE", "")
        if named_profile:
            values = _read_toml_flat(PROFILE_ROOT / f"{named_profile}.toml") \
                if (PROFILE_ROOT / f"{named_profile}.toml").is_file() else {}
            _apply_env(values)
            profile = os.environ.get("GMS_RT_PROFILE", "")
            client = os.environ.get("GMS_AGENT_CLIENT", "") or _client_from_profile(profile)
    if not client:
        print(
            "mcp_launcher: no agent client/profile declared. Set "
            "GMS_AGENT_CLIENT (kimi/codex/kkagent) or GMS_RT_PROFILE / "
            "GMS_AGENT_PROFILE in the MCP registration env block; the "
            "first-match filesystem probe was removed (12.txt P1).",
            file=sys.stderr,
        )
        # Still exec the server: it starts unconfigured and reports auth
        # errors per-call instead of hiding the misconfiguration here.
    elif client:
        load_profile(client)

    # 15.txt 审核 P1-1: FORCE service-token — setdefault() let an ambient
    # GMS_AGENT_AUTH_MODE=human/password from the parent shell leak through
    # and re-enable the password/elevation tools. Agents must never run in
    # password mode even if the profile is missing. GMS_AGENT_PROCESS=1 is
    # stamped ONLY here, so the MCP server gets an independent second
    # signal that cannot be widened by forging the auth-mode variable.
    os.environ["GMS_AGENT_AUTH_MODE"] = "service-token"
    os.environ["GMS_AGENT_PROCESS"] = "1"

    os.execv(
        sys.executable,
        [sys.executable, str(SCRIPT_DIR / "mcp_server.py"), *sys.argv[1:]],
    )


if __name__ == "__main__":
    raise SystemExit(main())
