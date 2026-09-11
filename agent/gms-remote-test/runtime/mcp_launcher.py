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
import re
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CLIENTS = ("kimi", "codex", "kkagent")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
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


def load_named_profile(profile: str, expected_client: str = "") -> bool:
    """Load exactly one named profile, optionally checking its client."""

    if not PROFILE_NAME_RE.fullmatch(profile):
        print(f"mcp_launcher: invalid profile name: {profile!r}", file=sys.stderr)
        return False
    candidate = PROFILE_ROOT / f"{profile}.toml"
    if not candidate.is_file():
        print(f"mcp_launcher: profile not found: {candidate}", file=sys.stderr)
        return False
    try:
        values = _read_toml_flat(candidate)
    except OSError as exc:
        print(f"mcp_launcher: cannot read profile {candidate}: {exc}", file=sys.stderr)
        return False
    declared_client = values.get("GMS_AGENT_CLIENT", "")
    if expected_client and declared_client != expected_client:
        print(
            f"mcp_launcher: profile {profile!r} belongs to "
            f"{declared_client or 'an unknown client'}, not {expected_client}",
            file=sys.stderr,
        )
        return False
    _apply_env(values)
    return True


def profile_candidates(client: str) -> list[Path]:
    """Return deterministic profile candidates for one client."""

    if not PROFILE_ROOT.is_dir():
        return []
    return sorted(PROFILE_ROOT.glob(f"{client}-*.toml"))


def load_profile(client: str) -> bool:
    """Load the sole profile for a client; fail closed when ambiguous."""

    candidates = profile_candidates(client)
    if len(candidates) != 1:
        if candidates:
            names = ", ".join(path.stem for path in candidates)
            print(
                f"mcp_launcher: multiple {client} profiles found ({names}); "
                "set GMS_RT_PROFILE or GMS_AGENT_PROFILE explicitly",
                file=sys.stderr,
            )
        else:
            print(f"mcp_launcher: no profile found for client {client}", file=sys.stderr)
        return False
    return load_named_profile(candidates[0].stem, client)


def _client_from_profile(profile: str) -> str:
    """Read the ``client =`` field out of <profile>.toml (fail closed)."""
    if not PROFILE_NAME_RE.fullmatch(profile):
        return ""
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
    profile = os.environ.get("GMS_RT_PROFILE", "") or os.environ.get(
        "GMS_AGENT_PROFILE", ""
    )
    # A named profile is authoritative. Previously an explicit profile was
    # ignored whenever GMS_AGENT_CLIENT was also present (the normal
    # installer registration), and load_profile(client) silently selected
    # the first sorted profile. That could route an agent to another
    # Controller on multi-profile hosts.
    if profile:
        if not client:
            client = _client_from_profile(profile)
        if not client or not load_named_profile(profile, client):
            return 2
    if not client:
        print(
            "mcp_launcher: no agent client/profile declared. Set "
            "GMS_AGENT_CLIENT (kimi/codex/kkagent) or GMS_RT_PROFILE / "
            "GMS_AGENT_PROFILE in the MCP registration env block; the "
            "first-match filesystem probe was removed (12.txt P1).",
            file=sys.stderr,
        )
        return 2
    elif not profile and not load_profile(client):
        # Explicit environment-only registrations remain supported. Without
        # both values, starting an unconfigured MCP server only defers the
        # same failure to every tool call and obscures the actual fix.
        if not (
            os.environ.get("GMS_REMOTE_TEST_SERVER")
            and os.environ.get("GMS_AUTH_TOKEN_FILE")
        ):
            return 2

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
