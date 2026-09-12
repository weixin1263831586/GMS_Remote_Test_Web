"""gms_agent.profile_store — the single source of truth for TOML profiles.

Profile storage/parsing/selection used to be implemented twice
(mcp_launcher.py 与 gms_agent.package_manager.py 各自维护 PROFILE_ROOT、
TOML 解析与 profile 选择)。本模块现在是唯一的 profile 存储实现（ADR 0003，
docs/architecture/adr/0003-agent-profile-store.md）；launcher、package
lifecycle、SDK 与 doctor 全部只经由它读写 profile。

Fail-closed 选择契约（多 Controller 编译服务器的 Agent 路由保证，ADR 0003）:
    0 profiles  → resolve_profile() 返回 None
    1 profile   → 返回它
    >1 profiles → 返回 None（调用方必须显式指定 GMS_RT_PROFILE /
                  GMS_AGENT_PROFILE / --profile）
绝不 sorted() 取第一个——那是静默把 Agent 路由到错误 Controller 的行为。

存储布局（数据专用 TOML，0600，无 shell 语义）:
    ~/.config/gms-agent/profiles/<profile>.toml   profile 本体
    ~/.local/state/gms-remote-test/<profile>.token 0600  service token
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
from pathlib import Path


CLIENTS = ("codex", "kimi", "kkagent")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

PROFILE_ROOT = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "gms-agent" / "profiles"

STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
) / "gms-remote-test"


def validate_profile_name(profile: str) -> bool:
    """A profile name must be a safe single path segment."""

    return bool(PROFILE_NAME_RE.fullmatch(profile or ""))


def profile_path(profile: str) -> Path:
    return PROFILE_ROOT / f"{profile}.toml"


def token_file(profile: str) -> Path:
    """Where this profile's Agent Service Token is persisted (0600)."""

    return STATE_DIR / f"{profile}.token"


def profile_name(client: str) -> str:
    """Legacy host-scoped profile name: <client>-<host>-<uid>.

    Kept only as a deterministic name for tests/diagnostics that build a
    sandbox profile without a Controller. `write_profile` no longer uses
    it: the install default is `default_profile_name()` — the profile
    identity includes the Controller, so two Controllers on one host get
    two separate profiles instead of silently overwriting each other.
    """

    host = socket.gethostname().split(".")[0]
    return f"{client}-{host}-{os.getuid()}"


def default_profile_name(client: str, server: str) -> str:
    """Install default profile name: <client>-<host>-<sha256(server)[:8]>.

    The Controller identity (a short digest of its base URL) is part of the
    profile identity: installing Controller A and Controller B for the same
    client on the same host must produce two distinct profiles
    (codex-build01-1a2b3c4d / codex-build01-9f8e7d6c), never a silent
    overwrite. A readable name can be chosen explicitly with
    `gms-agent install --profile NAME`.
    """

    host = socket.gethostname().split(".")[0]
    digest = hashlib.sha256(server.rstrip("/").encode("utf-8")).hexdigest()[:8]
    return f"{client}-{host}-{digest}"


def list_profiles() -> list[str]:
    """All stored profile names, deterministically sorted."""

    if not PROFILE_ROOT.is_dir():
        return []
    return sorted(path.stem for path in PROFILE_ROOT.glob("*.toml"))


def profile_candidates(client: str) -> list[Path]:
    """List profiles for a client WITHOUT guessing which one is active.

    Selection is the caller's decision (see resolve_profile); this only
    provides the deterministic candidate list for diagnostics/messages.
    """

    if not PROFILE_ROOT.is_dir():
        return []
    candidates = []
    for path in sorted(PROFILE_ROOT.glob("*.toml")):
        try:
            declared_client = load_profile(path.stem).get("client", "")
        except OSError:
            continue
        if declared_client == client:
            candidates.append(path)
    return candidates


def resolve_profile(client: str) -> Path | None:
    """Fail-closed profile selection for one client.

    Exactly one candidate → that profile; zero or multiple → None (the
    caller reports ambiguity and demands an explicit profile name).
    """

    candidates = profile_candidates(client)
    if len(candidates) == 1:
        return candidates[0]
    return None


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_profile_toml(
    profile: str, client: str, server: str, ca_cert: str,
    insecure: bool | None = None,
) -> Path:
    """Write <PROFILE_ROOT>/<profile>.toml (0600).

    Data-only TOML — no shell semantics, no `source`ing, no quoting
    ambiguity. The launcher (mcp_launcher.py), the
    lifecycle (package_manager.py) and the SDK all read it through this
    module only.
    """

    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    lines = [
        f'profile = "{toml_escape(profile)}"',
        f'client = "{toml_escape(client)}"',
        "",
        "[controller]",
        f'url = "{toml_escape(server)}"',
    ]
    if ca_cert:
        lines.append(f'ca_cert = "{toml_escape(ca_cert)}"')
    if insecure is None:
        insecure = os.environ.get("GMS_INSTALL_INSECURE", "") == "1"
    if insecure:
        # Install ran without a trusted CA (self-signed deployment); the
        # launcher maps this to GMS_CURL_INSECURE=1 for the runtime.
        lines.append("insecure = true")
    lines += [
        "",
        "[auth]",
        'mode = "service-token"',
        f'token_file = "{toml_escape(str(token_file(profile)))}"',
        "",
    ]
    path = profile_path(profile)
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    return path


def read_profile_toml(path: Path) -> dict[str, dict[str, str]]:
    """Minimal TOML reader for the fixed profile shape (top-level strings
    + [section] tables with string values). No external deps on py3.10."""

    result: dict[str, dict[str, str]] = {"": {}}
    section = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            result.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        result[section][key] = value
    return result


def load_profile(profile: str) -> dict[str, str]:
    """Flat view of one profile: profile/client/url/ca_cert/token_file/…"""

    path = profile_path(profile)
    if not path.is_file():
        return {}
    data = read_profile_toml(path)
    flat = dict(data.get("", {}))
    for section in ("controller", "auth"):
        flat.update(data.get(section, {}))
    return flat


def controller_url(flat: dict[str, str]) -> str:
    """Controller base URL from a load_profile() flat view."""

    return flat.get("url", "").rstrip("/")


def ca_cert(flat: dict[str, str]) -> str:
    """CA bundle path from a load_profile() flat view ("" = system trust)."""

    return flat.get("ca_cert", "")
