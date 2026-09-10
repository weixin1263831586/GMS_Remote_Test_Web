"""gms_agent.package_manager — the GMS agent package lifecycle (10.txt §二十九~§三十六).

Extracted from the gms-agent single-file installer: registry download +
Ed25519 manifest verification, safe extraction, immutable versions/<v>/
install with whole-package activation, TOML profiles and client adapters.
`gms-agent` (runtime/gms-agent) is the thin argparse shell; everything it
does lives here so the MCP/SDK layer can reuse the same logic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


# SCRIPT_DIR in the module context = the runtime/ directory (parent of the
# gms_agent package); the thin shell overrides nothing — local_package_root
# resolves the package root relative to it exactly like the old single file.
SCRIPT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path(
    os.environ.get(
        "GMS_AGENT_RUNTIME_ROOT",
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "gms-remote-test",
    )
)
VERSIONS_DIR = RUNTIME_ROOT / "versions"
CURRENT_LINK = RUNTIME_ROOT / "current"
MCP_ENV_DIR = RUNTIME_ROOT / "mcp"
STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
) / "gms-remote-test"

CLIENTS = ("codex", "kimi", "kkagent")
PACKAGE_SHA_MARKER = ".package-sha256"

# Safe-extract guard rails (10.txt §十九): the registry archive is
# semi-trusted transport (SHA-256 pre-verified) but extraction must still
# refuse anything that escapes the staging root.
MAX_ARCHIVE_FILES = 5000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def detect_clients() -> list[str]:
    found = []
    if Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).is_dir() or shutil.which("codex"):
        found.append("codex")
    if Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code")).is_dir() or shutil.which("kimi"):
        found.append("kimi")
    if Path(os.environ.get("KKAGENT_HOME", Path.home() / ".kkagent")).is_dir() or shutil.which("kkagent"):
        found.append("kkagent")
    return found


# ---------------------------------------------------------------------------
# Package identity
# ---------------------------------------------------------------------------

def cli_version_in(root: Path) -> str | None:
    """Read GMS_RT_VERSION from a package root.

    Both layouts carry gms-remote-test.sh one level below the root:
      distribution/generated layout: <root>/scripts/gms-remote-test.sh
      source layout (11.txt):         <root>/runtime/gms-remote-test.sh
    """
    for scripts_dir in ("scripts", "runtime"):
        marker = root / scripts_dir / "gms-remote-test.sh"
        if marker.is_file():
            for line in marker.read_text(encoding="utf-8").splitlines():
                if line.startswith("GMS_RT_VERSION="):
                    return line.split("=", 1)[1].strip().strip('"') or None
    return None


def local_package_root(arg_package: str, script_dir: Path | None = None) -> Path | None:
    """Locate the package root for `install`.

    Valid package roots contain gms-remote-test.sh one level below them:
    the extracted distribution archive (<root>/scripts/…), the generated
    plugin payload (plugins/gms-remote-test/) or — in the source tree
    (11.txt layout) — the agent package root itself
    (agent/gms-remote-test/, carrying runtime/…). A standalone gms-agent
    downloaded via GET /api/agent/install sits alone in e.g. /tmp and
    matches nothing — install then bootstraps from the registry.

    `script_dir` is the directory of the RUNNING gms-agent entry point
    (the thin shell passes its own location so a standalone download is
    detected by ITS surroundings, not the package_manager module's).
    """
    if arg_package:
        root = Path(arg_package).resolve()
        return root if cli_version_in(root) else None
    base = Path(script_dir).resolve() if script_dir else SCRIPT_DIR
    return root if cli_version_in(root := base.parent) else None


def package_tree_sha256(root: Path) -> str:
    """Stable content digest of a package tree (version != content identity
    is exactly what this pins; see 10.txt §二十一).

    The digest is computed over the CANONICAL distribution paths so a
    source-layout tree (agent/gms-remote-test, 11.txt) and its normalized
    versions/<v>/ copy produce the same value: runtime/… maps to scripts/…,
    skill/… maps to skills/gms-remote-test/…, manifests/* map to the plugin
    root. Non-runtime scaffolding is excluded from the digest so a source
    install and the registry zip of the same version share one content
    identity: docs/, tests/, package.yaml, GENERATED.md and the dev-only
    scripts/install_local.sh never change it.
    """
    normalized = normalize_package_tree(root)
    try:
        digest_root = normalized
        digest = hashlib.sha256()
        for path in sorted(digest_root.rglob("*")):
            rel_path = path.relative_to(digest_root)
            parts = rel_path.parts
            if "__pycache__" in parts or path.name == PACKAGE_SHA_MARKER:
                continue
            if path.name == "install_local.sh":
                continue
            if (parts and parts[0] in {"docs", "tests"}) or rel_path.as_posix() == "package.yaml":
                continue
            rel = rel_path.as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            if path.is_file():
                digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()
    finally:
        if normalized != root:
            shutil.rmtree(normalized, ignore_errors=True)


def runtime_version() -> str:
    """Version of the running gms-agent's own checkout (display only)."""
    return cli_version_in(SCRIPT_DIR.parent) or "unknown"


def installed_version() -> str | None:
    target = CURRENT_LINK.resolve() if CURRENT_LINK.exists() else None
    if target and (target / "scripts" / "gms-remote-test.sh").is_file():
        return target.name
    return None


# ---------------------------------------------------------------------------
# HTTP / registry
# ---------------------------------------------------------------------------

def server_url_from_env() -> str:
    url = (
        os.environ.get("GMS_REMOTE_TEST_SERVER", "")
        # Bootstrap installs embed the Controller URL at download time
        # (GET /api/agent/install); an explicit env var still wins.
        or "__GMS_AGENT_DEFAULT_SERVER__"
    )
    if not url or url.startswith("__GMS_"):
        print(
            "Error: GMS_REMOTE_TEST_SERVER 未设置（gms-agent 不会猜测 Controller 地址）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url.rstrip("/")


def http_get(url: str, ca_cert: str = "", timeout: int = 60) -> tuple[bytes, dict[str, str]]:
    context = None
    if url.startswith("https://"):
        import ssl

        if ca_cert:
            context = ssl.create_default_context(cafile=ca_cert)
        elif os.environ.get("GMS_INSTALL_INSECURE") == "1":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"User-Agent": "gms-agent-installer"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.read(), dict(response.headers)


def registry_base(server: str) -> str:
    return f"{server}/api/agent/packages/gms-remote-test"


def _manifest_signature_payload(manifest: dict) -> bytes:
    """Canonical bytes covered by the manifest Ed25519 signature:
    sha256 + version + size (the fields an attacker would need to swap)."""
    artifact = (manifest.get("artifacts") or {}).get("universal") or {}
    payload = "|".join([
        str(manifest.get("name", "")),
        str(manifest.get("version", "")),
        str(artifact.get("sha256", "")),
        str(artifact.get("size", "")),
    ])
    return payload.encode("utf-8")


def _verify_manifest_signature(manifest: dict, signature_b64: str, verify_key_b64: str) -> bool:
    """Verify the manifest signature with a pinned Ed25519 public key.

    cryptography is optional on the build host — when unavailable, signature
    verification cannot run and the caller's earlier SHA-256 check remains
    the integrity boundary; here we fail closed (configuring a verify key
    without cryptography installed is a deployment error).
    """
    import base64

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise RuntimeError(
            "已配置 GMS_AGENT_VERIFY_KEY_B64 但缺少 cryptography 库；"
            "请安装 cryptography 或取消该环境变量"
        ) from error
    try:
        pem = base64.b64decode(verify_key_b64)
        public_key = serialization.load_pem_public_key(pem)
        if not isinstance(public_key, Ed25519PublicKey):
            return False
        public_key.verify(
            base64.b64decode(signature_b64),
            _manifest_signature_payload(manifest),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def artifact_url_ok(url: object, server: str) -> bool:
    """Artifact must be http(s) AND same origin as the Controller (no
    off-host download redirects baked into a tampered manifest)."""
    if not isinstance(url, str):
        return False
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    origin = urlsplit(server)
    return parsed.netloc == origin.netloc


# ---------------------------------------------------------------------------
# Safe extraction (10.txt §十九)
# ---------------------------------------------------------------------------

def safe_extract(archive_path: Path, dest: Path) -> None:
    """Extract a package zip with path/symlink/bombing guards."""
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise ValueError(f"包内文件数超限（{len(entries)} > {MAX_ARCHIVE_FILES}）")
        total = sum(entry.file_size for entry in entries)
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError(f"包解压体积超限（{total} bytes）")
        for entry in entries:
            name = entry.filename
            if name.startswith("/") or "\\" in name or ":" in name:
                raise ValueError(f"非法包内路径: {name!r}")
            parts = PurePosixPath(name).parts
            if not parts or ".." in parts:
                raise ValueError(f"非法包内路径: {name!r}")
            target = (dest_resolved.joinpath(*parts)).resolve()
            if dest_resolved != target and dest_resolved not in target.parents:
                raise ValueError(f"包内路径越界: {name!r}")
            mode = entry.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type in (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
                raise ValueError(f"拒绝特殊文件条目: {name!r}")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # S_IFREG or type bits absent (permission-only attr, what our own
            # builder writes) are both regular file entries.
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            if mode & 0o111:
                target.chmod(target.stat().st_mode | 0o755)


# ---------------------------------------------------------------------------
# Registry download (bootstrap + update share this)
# ---------------------------------------------------------------------------

def fetch_registry_package(
    server: str,
    ca_cert: str,
) -> tuple[Path, Path, str, str]:
    """Download + verify the registry package; returns (root, staging, version, tree_sha256).

    Integrity chain (10.txt §二十): the manifest carries the artifact
    SHA-256 AND an optional Ed25519 signature (signed by the Controller's
    release key, GMS_SKILL_SIGNING_KEY_FILE). The client pins the release
    public key via GMS_AGENT_VERIFY_KEY_B64 (base64 SubjectPublicKeyInfo
    PEM, exactly what /api/system/skills/install.sh embeds) or trusts the
    pin baked in at bootstrap time (GMS_AGENT_VERIFY_KEY_B64 replacement —
    see agent_bootstrap_installer). When a key is configured, an absent or
    invalid signature is a hard failure.
    """
    try:
        raw, _headers = http_get(f"{registry_base(server)}/manifest", ca_cert)
        manifest = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"无法读取 Controller 包清单: {error}") from error
    latest = str(manifest.get("version", ""))
    if not latest:
        raise RuntimeError("Controller 清单缺少 version")
    artifact = (manifest.get("artifacts") or {}).get("universal") or {}
    artifact_url = artifact.get("url")
    expected_sha = str(artifact.get("sha256", ""))
    manifest_sig = str(manifest.get("signature", ""))
    if not artifact_url_ok(artifact_url, server):
        raise RuntimeError("清单的 universal 包下载地址非法（非同源 http/https）")
    if not expected_sha:
        raise RuntimeError("清单缺少 SHA-256；拒绝无完整性校验的包")
    try:
        data, _headers = http_get(artifact_url, ca_cert, timeout=600)
    except (urllib.error.URLError, OSError) as error:
        raise RuntimeError(f"包下载失败: {error}") from error
    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError("包 SHA-256 校验失败；拒绝安装")
    verify_key = os.environ.get(
        "GMS_AGENT_VERIFY_KEY_B64",
        # Pinned at bootstrap download time (agent_bootstrap_installer);
        # an explicit env var still wins. Empty = signature optional.
        "__GMS_AGENT_VERIFY_KEY_B64__",
    )
    if verify_key.startswith("__GMS_"):
        verify_key = ""
    if verify_key:
        if not manifest_sig:
            raise RuntimeError("清单缺少 Ed25519 签名；已配置验证密钥时签名强制")
        if not _verify_manifest_signature(manifest, manifest_sig, verify_key):
            raise RuntimeError("清单 Ed25519 签名校验失败；拒绝安装")

    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    staging = VERSIONS_DIR / f".download-{latest}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    archive_path = staging / "package.zip"
    archive_path.write_bytes(data)
    try:
        safe_extract(archive_path, staging)
        archive_path.unlink(missing_ok=True)
        root = staging / "gms-remote-test"
        if not (root / "scripts").is_dir():
            root = staging
        package_version = cli_version_in(root)
        if package_version != latest:
            raise RuntimeError(
                f"包版本契约不成立: manifest={latest} package={package_version}"
            )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # Identity uses the canonical TREE digest (package_tree_sha256), not the
    # zip bytes: a source-layout install and the registry zip of the same
    # version must map to ONE versions/<v>/ content identity. The zip-byte
    # SHA (actual_sha) was already verified against the manifest above.
    return root, staging, latest, package_tree_sha256(root)


# ---------------------------------------------------------------------------
# install / activation
# ---------------------------------------------------------------------------

def copytree_atomic(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


def flip_current(target: Path) -> None:
    # Atomic flip: symlink swap via os.replace on a temp link.
    tmp_link = CURRENT_LINK.with_name(f".current-{os.getpid()}")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(target)
    os.replace(tmp_link, CURRENT_LINK)


def normalize_package_tree(source_root: Path) -> Path:
    """Rewrite a SOURCE-layout package (agent/gms-remote-test, 11.txt) into
    the canonical DISTRIBUTION layout (runtime/ → scripts/, skill/ →
    skills/gms-remote-test/, manifests/* → plugin-root manifests).

    A staging tree is built and returned; already-canonical trees are
    returned unchanged. This keeps versions/<v>/, install_skill and the
    kkagent plugin installer layout-independent.
    """
    if (source_root / "scripts").is_dir() or not (source_root / "runtime").is_dir():
        return source_root
    staging = source_root.parent / f".normalized-{source_root.name}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(
        source_root / "runtime", staging / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        source_root / "skill", staging / "skills" / "gms-remote-test",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    manifests = source_root / "manifests"
    if manifests.is_dir():
        if (manifests / "kk.plugin.json").is_file():
            shutil.copy2(manifests / "kk.plugin.json", staging / "kk.plugin.json")
        if (manifests / "kimi.plugin.json").is_file():
            shutil.copy2(manifests / "kimi.plugin.json", staging / "kimi.plugin.json")
        if (manifests / "codex.plugin.json").is_file():
            target_dir = staging / ".codex-plugin"
            target_dir.mkdir()
            shutil.copy2(manifests / "codex.plugin.json", target_dir / "plugin.json")
    # Deliberately NOT copied: docs/, tests/, package.yaml, GENERATED.md —
    # the canonical distribution tree (agent_package_builder layout) carries
    # only scripts/ + skills/ + tests/ + root manifests, and keeping the
    # normalized tree byte-comparable to the registry zip is what makes the
    # package_tree_sha256 identity checks (versions/<v>/ immutability) agree
    # across both install sources.
    return staging


def install_runtime(source_root: Path, version: str, sha256: str) -> Path:
    """Copy the package into the IMMUTABLE versions/<version>/ directory and
    flip `current` atomically.

    Version directories are content-addressed by the recorded tree SHA: an
    existing directory with a different digest is an error (10.txt §二十一)
    — release a new version instead of mutating an installed one.

    Source-layout trees (agent/gms-remote-test) are normalized to the
    canonical distribution layout first; the normalization staging tree is
    removed afterwards.
    """
    normalized = normalize_package_tree(source_root)
    try:
        return _install_runtime_normalized(normalized, version, sha256)
    finally:
        if normalized != source_root:
            shutil.rmtree(normalized, ignore_errors=True)


def _install_runtime_normalized(source_root: Path, version: str, sha256: str) -> Path:
    target = VERSIONS_DIR / version
    marker = target / PACKAGE_SHA_MARKER
    if target.exists():
        existing = ""
        if marker.is_file():
            existing = marker.read_text(encoding="utf-8").strip()
        if existing and existing == sha256:
            flip_current(target)  # identical payload: reuse, no rewrite
            return target
        raise RuntimeError(
            f"版本目录 {target} 已存在且内容不一致；版本号不可复用"
            f"（existing={existing[:12]}… new={sha256[:12]}…）。请发布新版本号。"
        )
    staging = VERSIONS_DIR / f".staging-{version}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, staging, ignore=shutil.ignore_patterns("__pycache__"))
    (staging / PACKAGE_SHA_MARKER).write_text(sha256 + "\n", encoding="utf-8")
    staging.rename(target)
    flip_current(target)
    return target


def client_skill_root(client: str) -> Path:
    if client == "kimi":
        return Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code")) / "skills"
    if client == "kkagent":
        return Path(os.environ.get("KKAGENT_HOME", Path.home() / ".kkagent")) / "skills"
    return Path(
        os.environ.get("GMS_CODEX_SKILLS_DIR")
        or (Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills")
    )


def profile_name(client: str) -> str:
    host = socket.gethostname().split(".")[0]
    return f"{client}-{host}-{os.getuid()}"


# ---------------------------------------------------------------------------
# TOML profiles (10.txt §十八: 取代 export env + source 方案)
# ---------------------------------------------------------------------------

PROFILE_ROOT = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "gms-agent" / "profiles"


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_profile_toml(profile: str, client: str, server: str, ca_cert: str) -> Path:
    """Write ~/.config/gms-agent/profiles/<profile>.toml (0600).

    Data-only TOML — no shell semantics, no `source`ing, no quoting
    ambiguity (10.txt §十七/§十八). The launcher (mcp_launcher.py) and the
    SDK read it directly.
    """
    token_file = STATE_DIR / f"{profile}.token"
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    lines = [
        f'profile = "{_toml_escape(profile)}"',
        f'client = "{_toml_escape(client)}"',
        "",
        "[controller]",
        f'url = "{_toml_escape(server)}"',
    ]
    if ca_cert:
        lines.append(f'ca_cert = "{_toml_escape(ca_cert)}"')
    lines += [
        "",
        "[auth]",
        'mode = "service-token"',
        f'token_file = "{_toml_escape(str(token_file))}"',
        "",
    ]
    path = PROFILE_ROOT / f"{profile}.toml"
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
    """Flat view of a profile TOML: profile/client/server/ca_cert/token_file."""
    path = PROFILE_ROOT / f"{profile}.toml"
    if not path.is_file():
        return {}
    data = read_profile_toml(path)
    flat = dict(data.get("", {}))
    for section in ("controller", "auth"):
        flat.update(data.get(section, {}))
    return flat


def profile_server_and_ca(client: str) -> tuple[str, str]:
    """Re-read Controller URL / CA from an existing profile (update/rollback
    re-activation must not need the user to repeat --server). TOML first,
    legacy .env fallback for hosts installed before 0.14."""
    legacy_env = MCP_ENV_DIR / f"{client}.env"
    server, ca = "", ""
    if legacy_env.is_file():
        for line in legacy_env.read_text(encoding="utf-8").splitlines():
            if line.startswith("export GMS_REMOTE_TEST_SERVER="):
                parts = shlex.split(line.split("=", 1)[1])
                server = parts[0] if parts else ""
            elif line.startswith("export GMS_CURL_CA_CERT="):
                parts = shlex.split(line.split("=", 1)[1])
                ca = parts[0] if parts else ""
    if not server:
        # Find the TOML profile for this client (profile files are named
        # <client>-<host>-<uid>.toml).
        if PROFILE_ROOT.is_dir():
            for candidate in sorted(PROFILE_ROOT.glob(f"{client}-*.toml")):
                flat = load_profile(candidate.stem)
                if flat.get("profile"):
                    server = flat.get("url", "")
                    ca = flat.get("ca_cert", "")
                    break
    return server.rstrip("/"), ca


def write_profile(client: str, server: str, ca_cert: str) -> str:
    """Write the client profile in BOTH formats during migration (10.txt §十八):
    TOML is the authoritative store (read by mcp_launcher.py / SDK); the
    legacy <client>.env is kept for the shell launcher fallback until it is
    retired."""
    name = profile_name(client)
    write_profile_toml(name, client, server, ca_cert)
    token_file = STATE_DIR / f"{name}.token"
    MCP_ENV_DIR.mkdir(parents=True, exist_ok=True)
    # shlex.quote (NOT json.dumps): this file is `source`d by shell code;
    # JSON quoting is not shell escaping (10.txt §十七).
    lines = [
        f"export GMS_REMOTE_TEST_SERVER={shlex.quote(server)}",
        f"export GMS_RT_PROFILE={shlex.quote(name)}",
        f"export GMS_AUTH_TOKEN_FILE={shlex.quote(str(token_file))}",
        "export GMS_AGENT_AUTH_MODE=service-token",
    ]
    if ca_cert:
        lines.append(f"export GMS_CURL_CA_CERT={shlex.quote(ca_cert)}")
    env_file = MCP_ENV_DIR / f"{client}.env"
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)
    return name


def configured_clients() -> list[str]:
    """Clients this host already activated (profile env files exist)."""
    if not MCP_ENV_DIR.is_dir():
        return []
    return [c for c in CLIENTS if (MCP_ENV_DIR / f"{c}.env").is_file()]


def install_skill(client: str, runtime_dir: Path) -> Path:
    source_skill = runtime_dir / "skills" / "gms-remote-test"
    target = client_skill_root(client) / "gms-remote-test"
    copytree_atomic(source_skill, target)
    print(f"  Skill ({client}): {target}")
    return target


def install_plugin_for_kkagent(runtime_dir: Path) -> None:
    """Register the bundled plugin payload under ~/.kkagent/plugins.

    Canonical layout (agent_package_builder.py): the manifests sit at the
    package ROOT (versions/<v>/kk.plugin.json) — the package root IS the
    plugin payload. Legacy double-wrapped layouts are probed as fallback.
    """
    kkagent_home = Path(os.environ.get("KKAGENT_HOME", Path.home() / ".kkagent"))
    candidates = [
        runtime_dir,  # canonical: manifests at package root
        runtime_dir / "plugins" / "gms-remote-test",
        runtime_dir.parent / "plugins" / "gms-remote-test",
    ]
    payload = next((c for c in candidates if (c / "kk.plugin.json").is_file()), None)
    if payload is None:
        print("  MCP (kkagent): plugin payload not found in package; skipped", file=sys.stderr)
        return
    target = kkagent_home / "plugins" / "gms-remote-test"
    copytree_atomic(payload, target)
    print(f"  MCP (kkagent): plugin installed at {target}")


def reconcile_mcp(client: str, server: str, name: str, ca_cert: str) -> None:
    token_file = STATE_DIR / f"{name}.token"
    config_path = (
        client_skill_root(client).parent
        / ("mcp.json" if client == "kimi" else "config.toml")
    )
    result = subprocess.run(
        [
            sys.executable,
            str(CURRENT_LINK / "scripts" / "agent_mcp_config.py"),
            client,
            str(config_path),
            str(CURRENT_LINK / "scripts" / "mcp_server.py"),
            server,
            name,
            str(token_file),
            ca_cert,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  MCP ({client}): reconcile failed — {result.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  MCP ({client}): {result.stdout.strip()}")


def install_cli_dispatcher() -> Path:
    bin_dir = Path(os.environ.get("GMS_BIN_DIR", Path.home() / ".local/bin"))
    bin_dir.mkdir(parents=True, exist_ok=True)
    dispatcher = bin_dir / "gms-rt"
    dispatcher.write_text(
        "#!/usr/bin/env bash\n"
        f'exec bash "{CURRENT_LINK}/scripts/gms-remote-test.sh" "$@"\n',
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    return dispatcher


def activate_clients(clients: list[str], server: str, ca_cert: str) -> None:
    """Whole-package activation: skill + client registration + profile all
    derive from CURRENT_LINK, so update/rollback refresh them together
    with the runtime (10.txt §九/§十/§十一)."""
    for client in clients:
        print(f"Configuring {client}:")
        name = write_profile(client, server, ca_cert)
        install_skill(client, CURRENT_LINK)
        if client == "kkagent":
            install_plugin_for_kkagent(CURRENT_LINK)
        else:
            reconcile_mcp(client, server, name, ca_cert)


def cmd_install(args: argparse.Namespace) -> int:
    server = args.server or server_url_from_env()
    ca_cert = os.environ.get("GMS_INSTALL_CA_CERT", "")

    clients = detect_clients() if args.client == "auto" else [args.client]
    if not clients:
        print("未检测到 codex/kimi/kkagent；将仅安装运行时与 CLI。")

    source_root = local_package_root(args.package, script_dir=SCRIPT_DIR)
    staging_to_cleanup: Path | None = None
    try:
        if source_root is not None:
            version = cli_version_in(source_root) or "unknown"
            sha256 = package_tree_sha256(source_root)
        else:
            # Standalone bootstrap (downloaded via GET /api/agent/install):
            # no local package — fetch it from the registry (10.txt §四).
            print("未检测到本地包；从 Controller Agent Package Registry 引导下载…")
            try:
                source_root, staging_to_cleanup, version, sha256 = fetch_registry_package(
                    server, ca_cert
                )
            except RuntimeError as error:
                print(f"Error: {error}", file=sys.stderr)
                return 6

        print(f"Installing GMS Agent Runtime {version}")
        runtime_dir = install_runtime(source_root, version, sha256)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 7
    finally:
        if staging_to_cleanup is not None:
            shutil.rmtree(staging_to_cleanup, ignore_errors=True)
    print(f"  Runtime: {CURRENT_LINK} -> {runtime_dir}")

    dispatcher = install_cli_dispatcher()
    print(f"  CLI: {dispatcher}")

    if clients:
        activate_clients(clients, server, ca_cert)

    print("\nDetected & configured:", ", ".join(clients) if clients else "(none)")
    print("Next: create an enrollment code in the Controller web UI, then run:")
    print("  gms-agent enroll <CODE>")
    return 0


# ---------------------------------------------------------------------------
# update / rollback (whole-package activation)
# ---------------------------------------------------------------------------

def cmd_update(args: argparse.Namespace) -> int:
    server = args.server or server_url_from_env()
    ca_cert = os.environ.get("GMS_INSTALL_CA_CERT", "")
    current = installed_version() or runtime_version()
    try:
        raw, _headers = http_get(f"{registry_base(server)}/manifest", ca_cert)
        manifest = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"Error: 无法读取 Controller 包清单: {error}", file=sys.stderr)
        return 6
    latest = str(manifest.get("version", ""))
    if not latest:
        print("Error: Controller 清单缺少 version", file=sys.stderr)
        return 7
    if latest == current and not args.force:
        print(f"Already up to date: {current}")
        return 0
    print(f"Updating {current} -> {latest}")

    try:
        source_root, staging, version, sha256 = fetch_registry_package(server, ca_cert)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 6
    try:
        installed_dir = install_runtime(source_root, version, sha256)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 7
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"Installed: {CURRENT_LINK} -> {installed_dir}")

    # Whole-package activation: refresh skill / kkagent plugin / MCP config
    # from the NEW version for every previously configured client.
    clients = configured_clients()
    if clients:
        for client in clients:
            server_i, ca_i = profile_server_and_ca(client)
            name = write_profile(client, server_i or server, ca_i or ca_cert)
            install_skill(client, CURRENT_LINK)
            if client == "kkagent":
                install_plugin_for_kkagent(CURRENT_LINK)
            else:
                reconcile_mcp(client, server_i or server, name, ca_i or ca_cert)
        print(f"Re-activated: {', '.join(clients)}")
    print("Profiles and tokens preserved. Restart agents to pick up the new runtime.")
    print(f"Rollback anytime: gms-agent rollback {current}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    version = args.version
    target = VERSIONS_DIR / version
    if not (target / "scripts" / "gms-remote-test.sh").is_file():
        print(f"Error: 未安装版本 {version}", file=sys.stderr)
        return 2
    flip_current(target)
    print(f"Rolled back: {CURRENT_LINK} -> {target}")
    # Rollback is whole-package too: skill/plugin/MCP must follow the
    # symlink back to <version> (10.txt §十).
    clients = configured_clients()
    for client in clients:
        server_i, ca_i = profile_server_and_ca(client)
        name = write_profile(client, server_i or server_url_from_env(), ca_i)
        install_skill(client, CURRENT_LINK)
        if client == "kkagent":
            install_plugin_for_kkagent(CURRENT_LINK)
        else:
            reconcile_mcp(client, server_i, name, ca_i)
    if clients:
        print(f"Re-activated from {version}: {', '.join(clients)}")
    return 0


# ---------------------------------------------------------------------------
# enroll
# ---------------------------------------------------------------------------

def cmd_enroll(args: argparse.Namespace) -> int:
    server = args.server or server_url_from_env()
    code = args.code
    if not code:
        print("Error: 需要 one-shot enrollment code", file=sys.stderr)
        return 2
    data = json.dumps({"code": code}).encode("utf-8")
    request = urllib.request.Request(
        f"{server}/api/auth/agent-enroll",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    import ssl

    context = None
    if server.startswith("https://"):
        ca = os.environ.get("GMS_INSTALL_CA_CERT", "")
        if ca:
            context = ssl.create_default_context(cafile=ca)
        elif os.environ.get("GMS_INSTALL_INSECURE") == "1":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        print(f"Error: enrollment 失败: HTTP {error.code} {detail}", file=sys.stderr)
        return 4
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"Error: enrollment 失败: {error}", file=sys.stderr)
        return 6
    token = (body.get("token") or {}).get("token", "")
    scopes = (body.get("token") or {}).get("scopes", [])
    if not token:
        print("Error: 响应缺少 token", file=sys.stderr)
        return 7
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    # Host-identity model: one enrollment → one token shared by every
    # configured client profile (the audit trail separates clients via
    # GMS_AGENT_CLIENT). Enroll once per client identity by re-running with
    # dedicated profiles if per-agent revocation is ever required.
    written = []
    for client in configured_clients():
        env_file = MCP_ENV_DIR / f"{client}.env"
        profile_line = next(
            (line for line in env_file.read_text(encoding="utf-8").splitlines()
             if line.startswith("export GMS_RT_PROFILE=")), ""
        )
        if not profile_line:
            continue
        profile = shlex.split(profile_line.split("=", 1)[1])
        if not profile:
            continue
        token_file = STATE_DIR / f"{profile[0]}.token"
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        written.append(str(token_file))
    for path in written:
        print(f"Agent token enrolled (0600): {path}")
    if scopes:
        print(f"Scopes: {', '.join(scopes)}")
    if not written:
        print("Warning: 没有已配置的 client profile；先运行 gms-agent install", file=sys.stderr)
        return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(_args: argparse.Namespace) -> int:
    print(f"Runtime:      {runtime_version()} (installed: {installed_version() or 'none'})")
    print(f"Runtime root: {RUNTIME_ROOT}")
    print(f"Detected:     {', '.join(detect_clients()) or '(none)'}")
    for client in configured_clients():
        env_file = MCP_ENV_DIR / f"{client}.env"
        state = "configured" if env_file.is_file() else "not configured"
        token_ref = ""
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("export GMS_AUTH_TOKEN_FILE="):
                    parts = shlex.split(line.split("=", 1)[1])
                    ref = Path(parts[0]) if parts else None
                    token_ref = " + token" if ref and ref.is_file() else " + token MISSING"
        print(f"  {client}: {state}{token_ref}")
    return 0


