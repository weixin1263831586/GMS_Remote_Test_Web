"""gms_agent.package_manager — the GMS agent package lifecycle.

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
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from gms_agent import profile_store


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
# Profile storage & the service-token STATE_DIR live in gms_agent.profile_store
# (single source of truth, ADR 0003); this module only owns versions/<v>/ + the
# current link.

CLIENTS = ("codex", "kimi", "kkagent")
PACKAGE_SHA_MARKER = ".package-sha256"

# Safe-extract guard rails: the registry archive is
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
      source layout:                 <root>/runtime/gms-remote-test.sh
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
    plugin payload (plugins/gms-remote-test/) or — in the source tree —
    the agent package root itself
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
    is exactly what this pins).

    The digest is computed over the CANONICAL distribution paths so a
    source-layout tree (agent/gms-remote-test) and its normalized
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

def server_url_from_env(profile: str = "") -> str:
    return resolve_controller("", profile)[0]


def resolve_controller(explicit_server: str = "", explicit_profile: str = "") -> tuple[str, str]:
    """Resolve the (Controller URL, CA cert) for update/enroll/rollback.

    Fail-closed multi-Controller rule (ADR 0003) — precedence:

      1. an explicit ``--profile`` pins that profile's Controller;
      2. an explicit ``--server`` wins when no profile is selected;
      3. else the environment (``GMS_REMOTE_TEST_SERVER`` or the
         bootstrap-embedded URL);
      4. else the UNIQUE Controller across ALL installed profiles —
         zero profiles or more than one distinct Controller is an error,
         never a per-client first-match guess.

    A client with ambiguous profiles must NOT fall through to another
    client's sole Controller: the CA/token of Controller B must never be
    silently used against Controller A. Pass ``explicit_profile`` (or
    ``--server``) to disambiguate; the CA is returned only for an explicit
    profile whose trust store was recorded at install time.
    """

    requested_server = explicit_server.rstrip("/")
    if explicit_profile:
        flat = profile_store.load_profile(explicit_profile)
        profile_server = profile_store.controller_url(flat)
        if profile_server:
            if requested_server and requested_server != profile_server:
                print(
                    f"Error: --server {requested_server} 与 profile "
                    f"{explicit_profile} 的 Controller {profile_server} 不一致",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            return profile_server, profile_store.ca_cert(flat)
        print(
            f"Error: profile {explicit_profile} 不存在或缺少 Controller URL",
            file=sys.stderr,
        )
        raise SystemExit(2)
    server = (
        requested_server
        or os.environ.get("GMS_REMOTE_TEST_SERVER", "")
        # Bootstrap installs embed the Controller URL at download time
        # (GET /api/agent/install); an explicit env var still wins.
        or "__GMS_AGENT_DEFAULT_SERVER__"
    ).rstrip("/")
    if server and not server.startswith("__GMS_"):
        return server, ""
    unique_server, unique_ca = profile_server_and_ci_or_none()
    if unique_server:
        return unique_server, unique_ca
    print(
        "Error: GMS_REMOTE_TEST_SERVER 未设置且本机没有指向唯一 Controller 的"
        " profile（gms-agent 不会猜测 Controller 地址；"
        "请使用 --server URL 或 --profile NAME 指定）",
        file=sys.stderr,
    )
    raise SystemExit(2)


def profile_server_and_ci_or_none() -> tuple[str, str]:
    """Unique (server, ca) across ALL installed profiles — fail closed.

    Returns ("", "") when the host has zero profiles or more than one
    DISTINCT Controller: a corrupt/unreadable store is indistinguishable
    from "no profile", and a multi-Controller host must resolve the
    ambiguity explicitly (--server / --profile), never via a per-client
    first-match that let codex-ambiguous hosts silently route to kimi's
    Controller."""

    try:
        server = ""
        ca = ""
        for name in profile_store.list_profiles():
            flat = profile_store.load_profile(name)
            url = profile_store.controller_url(flat)
            if not url:
                continue
            if server and server != url:
                return "", ""
            if not server:
                server, ca = url, profile_store.ca_cert(flat)
        return server, ca
    except Exception:
        return "", ""


def http_get(
    url: str,
    ca_cert: str = "",
    timeout: int = 60,
    insecure: bool | None = None,
) -> tuple[bytes, dict[str, str]]:
    import ssl

    context = None
    if url.startswith("https://"):
        if ca_cert:
            context = ssl.create_default_context(cafile=ca_cert)
        elif (
            insecure
            if insecure is not None
            else os.environ.get("GMS_INSTALL_INSECURE") == "1"
        ):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        # urllib follows redirects by default, which lets
        # a same-origin endpoint bounce the package fetch to another host.
        # Registry downloads must never leave the pinned origin.
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    if isinstance(context, ssl.SSLContext):
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _NoRedirects()
        )
        open_fn = opener.open
    else:
        opener = urllib.request.build_opener(_NoRedirects())
        open_fn = opener.open
    request = urllib.request.Request(url, headers={"User-Agent": "gms-agent-installer"})
    with open_fn(request, timeout=timeout) as response:
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
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise RuntimeError(
            "已配置 GMS_AGENT_VERIFY_KEY_B64 但缺少 cryptography 库；"
            "请安装 cryptography 或取消该环境变量"
        ) from error
    try:
        pem = base64.b64decode(verify_key_b64)
        try:
            # cryptography < 3.1 requires an explicit backend; newer
            # releases accept (and ignore) it as well.
            public_key = serialization.load_pem_public_key(pem, backend=default_backend())
        except TypeError:
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
    off-host download redirects baked into a tampered manifest).

    Historically, netloc-only comparison let a manifest point an
    https origin at an http artifact URL (downgrade to plaintext). The
    scheme must match too; http_get() disables redirects so a same-origin
    endpoint cannot bounce the fetch off-host either.
    """
    if not isinstance(url, str):
        return False
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    origin = urlsplit(server)
    return (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc)


# ---------------------------------------------------------------------------
# Safe extraction
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
    expected_version: str = "",
    insecure: bool | None = None,
) -> tuple[Path, Path, str, str]:
    """Download + verify the registry package; returns (root, staging, version, tree_sha256).

    Integrity chain: the manifest carries the artifact
    SHA-256 AND an optional Ed25519 signature (signed by the Controller's
    release key, GMS_SKILL_SIGNING_KEY_FILE). The client pins the release
    public key via GMS_AGENT_VERIFY_KEY_B64 (base64 SubjectPublicKeyInfo
    PEM, exactly what the /api/agent/install bootstrap embeds) or trusts the
    pin baked in at bootstrap time (GMS_AGENT_VERIFY_KEY_B64 replacement —
    see agent_bootstrap_installer). When a key is configured, an absent or
    invalid signature is a hard failure.

    ``expected_version``: when the caller already fetched
    a manifest for its version decision (cmd_update), the package fetch must
    NOT read the manifest a second time — two reads let a racing registry
    serve "new version" for the comparison and then install a validly-signed
    OLD version (TOCTOU downgrade). The caller's version is passed through
    and pinned; a second fetch that disagrees is rejected.
    """
    try:
        raw, _headers = http_get(
            f"{registry_base(server)}/manifest", ca_cert, insecure=insecure
        )
        manifest = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"无法读取 Controller 包清单: {error}") from error
    latest = str(manifest.get("version", ""))
    if not latest:
        raise RuntimeError("Controller 清单缺少 version")
    if expected_version and latest != expected_version:
        raise RuntimeError(
            f"清单在版本判定与下载之间发生了变化（判定时 {expected_version}，"
            f"下载时 {latest}）；拒绝安装，请重试 update。"
        )
    artifact = (manifest.get("artifacts") or {}).get("universal") or {}
    artifact_url = artifact.get("url")
    expected_sha = str(artifact.get("sha256", ""))
    manifest_sig = str(manifest.get("signature", ""))
    if not artifact_url_ok(artifact_url, server):
        raise RuntimeError("清单的 universal 包下载地址非法（非同源 http/https）")
    if not expected_sha:
        raise RuntimeError("清单缺少 SHA-256；拒绝无完整性校验的包")
    try:
        data, _headers = http_get(
            artifact_url, ca_cert, timeout=600, insecure=insecure
        )
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
    """Rewrite a SOURCE-layout package (agent/gms-remote-test) into
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
    existing directory with a different digest is an error
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


# ---------------------------------------------------------------------------
# TOML profiles (取代 export env + source 方案)
#
# profile 存储/解析/选择收口到 gms_agent.profile_store（ADR 0003）——
# launcher、
# lifecycle 与 SDK 共享同一实现；本模块只保留生命周期语义（write_profile 的
# sticky-insecure 策略、update/rollback 的 fail-closed server 回读）。
# ---------------------------------------------------------------------------

# Storage primitives re-exported unchanged so existing callers (and tests)
# keep one import surface:
profile_name = profile_store.profile_name
profile_candidates = profile_store.profile_candidates
read_profile_toml = profile_store.read_profile_toml
load_profile = profile_store.load_profile
write_profile_toml = profile_store.write_profile_toml


def profile_server_and_ca(client: str) -> tuple[str, str]:
    """Re-read Controller URL / CA from the client's TOML profile
    (update/rollback re-activation must not need the user to repeat
    --server).

    Fail-closed (ADR 0003): with zero or multiple profiles for this client
    this returns ("", "") — a multi-Controller host must resolve the
    ambiguity via GMS_REMOTE_TEST_SERVER / an explicit profile, never via
    sorted()-first guessing. The legacy <client>.env fallback was removed
    together with the launcher's first-.env-wins glob."""

    selected = profile_store.resolve_profile(client)
    if selected is None:
        return "", ""
    flat = profile_store.load_profile(selected.stem)
    if not flat.get("profile"):
        return "", ""
    return profile_store.controller_url(flat), profile_store.ca_cert(flat)


def _profile_insecure(name: str) -> bool:
    """Sticky insecure flag: once a profile was written with the
    TLS-fallback policy, later re-activations (update/rollback)
    keep it even when GMS_INSTALL_INSECURE is unset."""

    path = profile_store.profile_path(name)
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line == "insecure = true":
                return True
    return os.environ.get("GMS_INSTALL_INSECURE", "") == "1"


def write_profile(client: str, server: str, ca_cert: str, profile: str = "") -> str:
    """Write the client profile (TOML only).

    ~/.config/gms-agent/profiles/<profile>.toml (0600) is the single
    authoritative store, read by mcp_launcher.py / the SDK / the CLI. The
    legacy <client>.env duplicate was removed: two stores invited
    first-match drift with multiple Controllers.

    The default profile identity includes the Controller
    (default_profile_name(): <client>-<host>-<sha256(server)[:8]>) so two
    Controllers for one client on one host occupy two profile files instead
    of silently overwriting each other; ``profile`` pins an explicit name
    (gms-agent install --profile NAME).

    See docs/architecture/adr/0003-agent-profile-store.md.
    """

    name = profile or profile_store.default_profile_name(client, server)
    if not profile_store.validate_profile_name(name):
        raise ValueError(f"非法 profile 名: {name!r}")
    insecure = _profile_insecure(name)
    profile_store.write_profile_toml(name, client, server, ca_cert, insecure)
    return name


def configured_clients() -> list[str]:
    """Clients this host already activated (TOML profile present)."""

    found = []
    for client in CLIENTS:
        if profile_store.profile_candidates(client):
            found.append(client)
    return found


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


def _mcp_registration_status(client: str) -> dict[str, object]:
    """Inspect only the client's GMS MCP registration marker."""

    if client == "kimi":
        path = client_skill_root(client).parent / "mcp.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            block = (payload.get("mcpServers") or {}).get("gms") or {}
            args = block.get("args") or []
            registered = any("mcp_launcher.py" in str(item) for item in args)
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
        registered = marker in text and "mcp_launcher.py" in text
        if client == "codex" and not registered:
            # Codex native plugins own their MCP registration through the
            # plugin manifest, so no standalone [mcp_servers.*] block is
            # expected. Recognize an enabled personal/team marketplace entry.
            plugin_block = re.search(
                r'(?ms)^\[plugins\."gms-remote-test@[^"\n]+"\]\s*'
                r'(?P<body>.*?)(?=^\[|\Z)',
                text,
            )
            registered = bool(
                plugin_block
                and re.search(
                    r"(?m)^enabled\s*=\s*true\s*$", plugin_block.group("body")
                )
            )
    return {"registered": registered, "config_path": str(path)}


def doctor_report(client: str = "auto", profile: str = "") -> dict[str, object]:
    """Build a secret-free local installation/profile consistency report."""

    if profile and re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
        declared = load_profile(profile).get("client", "")
    else:
        declared = ""
    clients = (
        [declared]
        if client == "auto" and declared in CLIENTS
        else (
            sorted(set(configured_clients()) | set(detect_clients()))
            if client == "auto"
            else [client]
        )
    )
    installed = installed_version() or ""
    running = runtime_version()
    current_cli = cli_version_in(CURRENT_LINK) or ""
    report_clients: list[dict[str, object]] = []
    actions: list[str] = []

    for client_name in clients:
        candidates = profile_candidates(client_name)
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
        if selected:
            flat = load_profile(selected.stem)
            server = flat.get("url", "")
            ca_cert = flat.get("ca_cert", "")
            token_state = _token_file_status(flat.get("token_file", ""))
            controller_valid = _controller_url_valid(server)
            profile_state.update(
                {
                    "controller": server,
                    "controller_url_valid": controller_valid,
                    "ca_configured": bool(ca_cert),
                    "ca_present": bool(ca_cert and Path(ca_cert).is_file()),
                    "valid": bool(controller_valid),
                }
            )
            if not controller_valid:
                actions.append(
                    f"replace the invalid Controller URL in profile {selected.stem}"
                )
            if ca_cert and not Path(ca_cert).is_file():
                actions.append(f"restore the Controller CA file for {client_name}")
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

        mcp_state = _mcp_registration_status(client_name)
        if not mcp_state["registered"]:
            actions.append(f"reconcile the {client_name} MCP registration")
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

    versions_consistent = bool(installed and installed == current_cli == running)
    if not versions_consistent:
        actions.append("install/activate one complete package version")
    unique_actions = list(dict.fromkeys(actions))
    ok = versions_consistent and all(
        bool(item["skill_present"])
        and bool(item["profile"]["valid"])
        and bool(item["token"]["present"])
        and bool(item["token"]["mode_ok"])
        and bool(item["token"]["owner_ok"])
        and bool(item["mcp"]["registered"])
        for item in report_clients
    )
    return {
        "ok": ok,
        "versions": {
            "running": running,
            "installed": installed or None,
            "current_cli": current_cli or None,
            "consistent": versions_consistent,
        },
        "runtime_root": str(RUNTIME_ROOT),
        "clients": report_clients,
        "actions": unique_actions,
    }


def install_skill(client: str, runtime_dir: Path) -> Path:
    source_skill = runtime_dir / "skills" / "gms-remote-test"
    target = client_skill_root(client) / "gms-remote-test"
    copytree_atomic(source_skill, target)
    print(f"  Skill ({client}): {target}")
    return target


def kkagent_plugin_registry() -> Path:
    return Path(os.environ.get("KKAGENT_HOME", Path.home() / ".kkagent")) / "plugins" / "installed.json"


def register_kkagent_plugin(target: Path) -> None:
    """Register the copied payload in ~/.kkagent/plugins/installed.json.

    kkagent discovers local plugins through the registry
    file, not by directory presence. install_plugin_for_kkagent used to stop
    at copying the payload, so `--client kkagent` installs were invisible to
    the host. Mirrors plugins/gms-remote-test/scripts/install_local.sh.
    """
    registry = kkagent_plugin_registry()
    version = ""
    manifest = target / "kk.plugin.json"
    if manifest.is_file():
        try:
            version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
        except ValueError:
            version = ""
    # A corrupt registry must never be silently replaced
    # with {} — one bad byte would wipe every OTHER plugin's registration on
    # the next install. Fail closed (back up the broken file, abort) so the
    # user can decide; the write itself goes through temp-file + os.replace
    # so a crash mid-write cannot corrupt the registry either.
    data: dict = {}
    if registry.is_file():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            backup = registry.with_name(
                f"installed.json.corrupt.{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            )
            try:
                backup.write_bytes(registry.read_bytes())
                backup_info = f"（已备份到 {backup}）"
            except OSError:
                backup_info = ""
            raise RuntimeError(
                f"{registry} 无法解析（{error}）；拒绝覆盖其他插件的登记记录{backup_info}。"
                "请修复或删除该文件后重试。"
            ) from error
        if not isinstance(data, dict):
            raise RuntimeError(f"{registry} 顶层不是 JSON 对象；拒绝覆盖。")
    plugins = data.setdefault("plugins", [])
    if not isinstance(plugins, list):
        plugins = data["plugins"] = []
    now = datetime.now(timezone.utc).isoformat()
    entry = next((p for p in plugins if isinstance(p, dict) and p.get("id") == "gms-remote-test"), None)
    if entry is None:
        entry = {"id": "gms-remote-test", "source": "local", "enabled": True}
        plugins.append(entry)
    entry.update({
        "root": str(target),
        "source": entry.get("source", "local"),
        "enabled": entry.get("enabled", True),
        "updatedAt": now,
        "version": version,
    })
    if "installedAt" not in entry:
        entry["installedAt"] = now
    registry.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    # Atomic replace: temp file + os.replace so a crash
    # mid-write leaves the previous registry intact.
    tmp = registry.with_name(f".installed.json.{os.getpid()}")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, registry)


def install_plugin_for_kkagent(runtime_dir: Path) -> None:
    """Register the bundled plugin payload under ~/.kkagent/plugins/local.

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
    # Install into plugins/local/<id> — the location the
    # local-plugin registry (and install_local.sh) use — not bare plugins/.
    target = kkagent_home / "plugins" / "local" / "gms-remote-test"
    copytree_atomic(payload, target)
    register_kkagent_plugin(target)
    print(f"  MCP (kkagent): plugin installed at {target}")


def reconcile_mcp(client: str, server: str, name: str, ca_cert: str) -> None:
    token_file = profile_store.token_file(name)
    config_path = (
        client_skill_root(client).parent
        / ("mcp.json" if client == "kimi" else "config.toml")
    )
    # 注册的启动命令必须是 mcp_launcher.py（它强制
    # GMS_AGENT_AUTH_MODE=service-token），而不是直接 exec mcp_server.py——
    # 后者会绕过 launcher 的安全边界，重新暴露密码/提权/审批工具。
    result = subprocess.run(
        [
            sys.executable,
            str(CURRENT_LINK / "scripts" / "agent_mcp_config.py"),
            client,
            str(config_path),
            str(CURRENT_LINK / "scripts" / "mcp_launcher.py"),
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


def install_cli_dispatcher() -> list[Path]:
    """Install the CLI dispatcher plus one command link per gms-rt-* function.

    The shell dispatcher resolves its command from $1 — it
    never looks at argv[0] — so a fresh install that only drops a single
    `gms-rt` file leaves every advertised command (gms-rt-system-health,
    gms-rt-agent-enroll, …) and the gms-agent lifecycle CLI unusable. The
    dispatcher below therefore accepts BOTH invocation styles:

        gms-rt gms-rt-devices-list ...    (explicit subcommand)
        gms-rt-devices-list ...           (argv0 via per-command symlink)

    and the installer creates one symlink per command function found in the
    INSTALLED dispatcher plus the `gms-agent` entry point — the surface the
    one-line /api/agent/install.sh flow relies on.
    """
    bin_dir = Path(os.environ.get("GMS_BIN_DIR", Path.home() / ".local/bin"))
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = CURRENT_LINK / "scripts" / "gms-remote-test.sh"
    dispatcher = bin_dir / "gms-rt"
    dispatcher.write_text(
        "#!/usr/bin/env bash\n"
        "# GMS Remote Test CLI dispatcher (generated by gms-agent install).\n"
        'invoked="${0##*/}"\n'
        'if [ "$invoked" = "gms-rt" ] && [ "$#" -gt 0 ] && [[ "$1" == gms-rt-* ]]; then\n'
        f'    exec bash "{cli}" "$@"\n'
        "fi\n"
        'if [[ "$invoked" == gms-rt-* ]]; then\n'
        f'    exec bash "{cli}" "$invoked" "$@"\n'
        "fi\n"
        f'    exec bash "{cli}" "$@"\n',
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    created = [dispatcher]
    links: dict[str, Path] = {"gms-agent": CURRENT_LINK / "scripts" / "gms-agent"}
    if cli.is_file():
        # gms-rt-* links must go through the dispatcher: gms-remote-test.sh
        # resolves its command from $1, never from argv0.
        for match in re.finditer(r"^(gms-rt-[a-z0-9-]+)\(\)", cli.read_text(encoding="utf-8"), re.M):
            links.setdefault(match.group(1), dispatcher)
    # Update/rollback can change the public command inventory. Remove only
    # stale links that this installer owns; never touch a user-managed file
    # or a symlink with a different target.
    for candidate in bin_dir.glob("gms-rt-*"):
        if candidate.name in links or not candidate.is_symlink():
            continue
        if Path(os.readlink(candidate)) == dispatcher:
            candidate.unlink()
    for link_name in sorted(links):
        link = bin_dir / link_name
        # The gms-agent lifecycle CLI is a real argparse
        # program (scripts/gms-agent) — linking it to the gms-rt dispatcher
        # made `gms-agent status/enroll/update` die with "Unknown command:
        # status" right after install. Each link must resolve to the target
        # recorded for it: gms-agent → the CLI entry point, gms-rt-* → the
        # dispatcher (argv0 subcommand resolution). See
        # docs/architecture/adr/0003-agent-profile-store.md.
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(links[link_name])
        created.append(link)
    return created


# ---------------------------------------------------------------------------
# Lifecycle file lock
# ---------------------------------------------------------------------------

def lifecycle_lock():
    """Cross-process advisory lock serializing install/update/rollback.

    Two concurrent updates would race on staging trees, versions/<v>/ and
    client configs. The lock is a plain 0600 file under STATE_DIR guarded
    by fcntl.flock (auto-released on process exit — a crashed installer
    cannot deadlock the next run).
    """
    from contextlib import contextmanager

    @contextmanager
    def _lock():
        import fcntl

        state_dir = profile_store.STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = state_dir / "lifecycle.lock"
        handle = open(lock_path, "w")  # noqa: SIM115 — held for the critical section
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with suppress_oserror():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    return _lock()


class suppress_oserror:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)


def activate_clients(
    clients: list[str], server: str, ca_cert: str, profile: str = "",
) -> list[str]:
    """Whole-package activation: skill + client registration + profile all
    derive from CURRENT_LINK, so update/rollback refresh them together
    with the runtime.

    Per-client failures no longer strand the host in a
    mixed state — the first failure aborts activation (install fails
    loudly; update keeps the new runtime but reports which client broke
    and exits non-zero).

    Returns the written profile names (install hands them to the enroll
    step so a one-shot code lands in exactly the profile just activated).
    """

    written: list[str] = []
    for client in clients:
        print(f"Configuring {client}:")
        name = write_profile(client, server, ca_cert, profile)
        written.append(name)
        install_skill(client, CURRENT_LINK)
        if client == "kkagent":
            install_plugin_for_kkagent(CURRENT_LINK)
        else:
            reconcile_mcp(client, server, name, ca_cert)
    return written


def reactivate_clients(
    server: str,
    ca_cert: str,
    previous_target: Path | None = None,
    allow_server_fallback: bool = False,
    profile: str = "",
) -> list[str]:
    """Transactional whole-package re-activation for update/rollback.

    Per-client Controller URL/CA are re-read from the existing profile and
    resolved ONCE (profile value, else the command-line/environment
    fallback) so write_profile and reconcile_mcp can never disagree — the
    old rollback path passed the UNresolved ``server_i`` to
    reconcile_mcp(), writing an empty server into the MCP config.

    ``server`` is only a FALLBACK for clients whose profile lacks a URL:
    with ``allow_server_fallback=False`` (update/rollback default) a
    profile-less URL makes the activation fail loudly instead of silently
    pointing the client at a controller chosen by the environment.

    ``previous_target`` is the versions/<v>/ directory the caller came
    FROM (before install_runtime flipped current). Activation failure
    triggers compensating rollback: current flips back there and the
    clients are re-activated from it, so the host returns to a consistent
    whole-package state instead of the mixed runtime/skill/plugin state a
    plain abort would leave. When omitted, the current link at entry is
    restored (plain activation retry semantics).
    """
    clients = configured_clients()
    if not clients:
        return []
    restore_target = previous_target or (
        Path(os.readlink(CURRENT_LINK)) if CURRENT_LINK.is_symlink() else None
    )
    restore_version = restore_target.name if restore_target else installed_version()
    try:
        for client in clients:
            selected = ""
            if profile:
                explicit = profile_store.load_profile(profile)
                if explicit.get("client") == client:
                    selected = profile
            if not selected:
                candidate = profile_store.resolve_profile(client)
                selected = candidate.stem if candidate else ""
            selected_flat = profile_store.load_profile(selected) if selected else {}
            profile_server = profile_store.controller_url(selected_flat)
            profile_ca = profile_store.ca_cert(selected_flat)
            resolved_server = profile_server or (server if allow_server_fallback else "")
            resolved_ca = profile_ca or ca_cert
            if not resolved_server:
                raise RuntimeError(
                    f"{client}: 无法确定 Controller URL"
                    f"（profile 缺失且未提供 --server{'，update/rollback 不使用环境回退' if not allow_server_fallback else ''}）"
                )
            print(f"Re-activating {client}:")
            name = write_profile(client, resolved_server, resolved_ca, selected)
            install_skill(client, CURRENT_LINK)
            if client == "kkagent":
                install_plugin_for_kkagent(CURRENT_LINK)
            else:
                reconcile_mcp(client, resolved_server, name, resolved_ca)
    except (SystemExit, Exception) as error:
        # reconcile_mcp reports failures via SystemExit(1); everything else
        # via ordinary exceptions. Both mean "this activation is broken" —
        # compensate back to the previous whole-package state and re-raise.
        print(
            f"Error: 激活失败（{error}）；回滚到 {restore_version or restore_target}",
            file=sys.stderr,
        )
        if restore_target is not None and (restore_target / "scripts" / "gms-remote-test.sh").is_file():
            flip_current(restore_target)
            try:
                for client in clients:
                    selected = ""
                    if profile:
                        explicit = profile_store.load_profile(profile)
                        if explicit.get("client") == client:
                            selected = profile
                    if not selected:
                        candidate = profile_store.resolve_profile(client)
                        selected = candidate.stem if candidate else ""
                    selected_flat = profile_store.load_profile(selected) if selected else {}
                    profile_server = profile_store.controller_url(selected_flat)
                    profile_ca = profile_store.ca_cert(selected_flat)
                    server_c = profile_server or (server if allow_server_fallback else "")
                    if not server_c:
                        print(
                            f"  MCP ({client}): 无法确定 Controller URL，补偿仅刷新本地文件",
                            file=sys.stderr,
                        )
                    install_skill(client, CURRENT_LINK)
                    if client == "kkagent":
                        install_plugin_for_kkagent(CURRENT_LINK)
                    elif server_c:
                        name = write_profile(
                            client, server_c, profile_ca or ca_cert, selected
                        )
                        reconcile_mcp(client, server_c, name, profile_ca or ca_cert)
            except (Exception, SystemExit) as rollback_error:  # best effort
                print(
                    f"Error: 补偿回滚也失败（{rollback_error}）；"
                    f"请手动执行 gms-agent rollback {restore_version}",
                    file=sys.stderr,
                )
        raise
    return clients


def cmd_install(args: argparse.Namespace) -> int:
    with lifecycle_lock():
        return _cmd_install_locked(args)


def _cmd_install_locked(args: argparse.Namespace) -> int:
    ca_cert = os.environ.get("GMS_INSTALL_CA_CERT", "")
    profile = (getattr(args, "profile", "") or "").strip()
    if profile and not profile_store.validate_profile_name(profile):
        print(f"Error: 非法 profile 名: {profile!r}", file=sys.stderr)
        return 2
    source_root = local_package_root(args.package, script_dir=SCRIPT_DIR)
    clients = (
        []
        if args.client == "none"
        else (detect_clients() if args.client == "auto" else [args.client])
    )
    if profile and len(clients) > 1:
        print(
            "Error: --profile 只能绑定一个客户端；请用 --client 指定 "
            "codex、kimi 或 kkagent 后分别安装",
            file=sys.stderr,
        )
        return 2
    install_server = (args.server or "").rstrip("/")
    if profile and install_server:
        existing_profile = profile_store.load_profile(profile)
        existing_server = profile_store.controller_url(existing_profile)
        if existing_server and existing_server != install_server:
            print(
                f"Error: profile {profile} 已绑定 {existing_server}；"
                "拒绝保留旧 token 后改绑其他 Controller",
                file=sys.stderr,
            )
            return 2
        if existing_server:
            ca_cert = profile_store.ca_cert(existing_profile)
    if not clients:
        if args.client == "none":
            print("已选择 --client none；将仅安装运行时与 CLI。")
        else:
            print("未检测到 codex/kimi/kkagent；将仅安装运行时与 CLI。")
    server = args.server or ""
    if source_root is None or clients:
        if not server:
            server, profile_ca = resolve_controller("", profile)
            if profile:
                ca_cert = profile_ca
            else:
                ca_cert = ca_cert or profile_ca
        if not _controller_url_valid(server):
            print(
                "Error: Controller URL 无效；要求 http(s)://host[:port]，"
                "且不能包含凭据、路径、查询或 shell 表达式",
                file=sys.stderr,
            )
            return 2

    staging_to_cleanup: Path | None = None
    try:
        if source_root is not None:
            version = cli_version_in(source_root) or "unknown"
            sha256 = package_tree_sha256(source_root)
        else:
            # Standalone bootstrap (downloaded via GET /api/agent/install):
            # no local package — fetch it from the registry.
            print("未检测到本地包；从 Controller Agent Package Registry 引导下载…")
            try:
                source_root, staging_to_cleanup, version, sha256 = fetch_registry_package(
                    server,
                    ca_cert,
                    insecure=(
                        profile_store.load_profile(profile).get("insecure") == "true"
                        if profile else None
                    ),
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

    cli_links = install_cli_dispatcher()
    print(f"  CLI: {cli_links[0]} (+{len(cli_links) - 1} gms-rt-*/gms-agent command links)")

    written_profiles: list[str] = []
    if clients:
        written_profiles = activate_clients(clients, server, ca_cert, profile)

    print("\nDetected & configured:", ", ".join(clients) if clients else "(none)")

    if not clients:
        print("Runtime/CLI installation complete; no client profile was changed.")
        return 0

    enroll_code = (getattr(args, "enroll_code", "") or "").strip()
    if not enroll_code:
        print("Next: create an enrollment code in the Controller web UI, then run:")
        if len(written_profiles) == 1:
            print(f"  gms-agent enroll <CODE> --profile {written_profiles[0]}")
        else:
            print("  gms-agent enroll <CODE>")
        return 0
    print("\nEnrolling provided one-shot code ...")
    args.code = enroll_code
    return cmd_enroll(args)


# ---------------------------------------------------------------------------
# update / rollback (whole-package activation)
# ---------------------------------------------------------------------------

def cmd_update(args: argparse.Namespace) -> int:
    with lifecycle_lock():
        return _cmd_update_locked(args)


def _cmd_update_locked(args: argparse.Namespace) -> int:
    profile = (getattr(args, "profile", "") or "").strip()
    server, profile_ca = resolve_controller(args.server, profile)
    ca_cert = (
        profile_ca
        if profile
        else os.environ.get("GMS_INSTALL_CA_CERT", "") or profile_ca
    )
    profile_insecure = (
        profile_store.load_profile(profile).get("insecure") == "true"
        if profile else None
    )
    current = installed_version() or runtime_version()
    try:
        raw, _headers = http_get(
            f"{registry_base(server)}/manifest", ca_cert, insecure=profile_insecure
        )
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

    def _version_tuple(value: str) -> tuple[int, ...]:
        parts = []
        for part in str(value).split("."):
            digits = ""
            for char in part:
                if char.isdigit():
                    digits += char
                else:
                    break
            parts.append(int(digits or 0))
        return tuple(parts) or (0,)

    # A stale-but-validly-signed
    # manifest must not be able to DOWNGRADE the host behind the user's
    # back — downgrade is an explicit `gms-agent rollback <version>`
    # decision. This holds for --force too: forcing only bypasses the
    # "already up to date" short-circuit, never the downgrade guard.
    if _version_tuple(latest) < _version_tuple(current):
        print(
            f"Error: 清单版本 {latest} 低于已安装版本 {current}；"
            "拒绝通过 update 降级（如需回退请使用 gms-agent rollback）",
            file=sys.stderr,
        )
        return 5
    print(f"Updating {current} -> {latest}")

    try:
        # Pin the version decided above into the package
        # fetch so a racing manifest cannot swap the artifact between the
        # comparison and the download (TOCTOU downgrade).
        source_root, staging, version, sha256 = fetch_registry_package(
            server,
            ca_cert,
            expected_version=latest,
            insecure=profile_insecure,
        )
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
    cli_links = install_cli_dispatcher()
    print(f"CLI refreshed: {len(cli_links) - 1} gms-rt-*/gms-agent command links")

    # Whole-package activation: refresh skill / kkagent plugin / MCP config
    # from the NEW version for every previously configured client.
    # Transactional — a client activation failure flips
    # current back to the PRE-update version and re-activates the clients
    # from it (no mixed runtime/skill/plugin state), then the error
    # propagates.
    try:
        # Update re-activation is profile-authoritative: a client whose
        # profile lacks a Controller URL fails the activation loudly instead
        # of silently inheriting the environment/--server value (which may
        # belong to another Controller on a multi-Controller host).
        reactivated = reactivate_clients(
            server,
            ca_cert,
            previous_target=VERSIONS_DIR / current,
            profile=profile,
        )
    except (SystemExit, Exception):
        # reactivate_clients already compensated back to the whole-package
        # {current} state and printed the underlying error.
        print(
            f"Error: 部分客户端激活失败；已整体回滚到 {current}（运行时与客户端一致）",
            file=sys.stderr,
        )
        return 1
    if reactivated:
        print(f"Re-activated: {', '.join(reactivated)}")
    print("Profiles and tokens preserved. Restart agents to pick up the new runtime.")
    print(f"Rollback anytime: gms-agent rollback {current}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    with lifecycle_lock():
        return _cmd_rollback_locked(args)


def _cmd_rollback_locked(args: argparse.Namespace) -> int:
    version = args.version
    target = VERSIONS_DIR / version
    if not (target / "scripts" / "gms-remote-test.sh").is_file():
        print(f"Error: 未安装版本 {version}", file=sys.stderr)
        return 2
    server, ca_cert = "", ""
    explicit_server = (getattr(args, "server", "") or "").strip()
    explicit_profile = (getattr(args, "profile", "") or "").strip()
    if explicit_server or explicit_profile:
        # Resolve before flipping current: invalid/conflicting routing input
        # must leave the installed package untouched.
        server, ca_cert = resolve_controller(explicit_server, explicit_profile)

    # Rollback never requires the global GMS_REMOTE_TEST_SERVER: TOML
    # profiles are the authoritative per-client store. The env value is
    # only a tolerant fallback; a client with NEITHER fails the activation
    # below (with compensation) instead of writing an empty server.
    rollback_from = installed_version()
    flip_current(target)
    print(f"Rolled back: {CURRENT_LINK} -> {target}")
    cli_links = install_cli_dispatcher()
    print(f"CLI refreshed: {len(cli_links) - 1} gms-rt-*/gms-agent command links")
    # Rollback is whole-package too: skill/plugin/MCP must follow the
    # symlink back to <version>. Per-client
    # profile values are resolved ONCE inside reactivate_clients(), so
    # reconcile_mcp can no longer receive an empty server while
    # write_profile got the environment fallback.
    previous_target = VERSIONS_DIR / rollback_from if rollback_from else None
    try:
        reactivated = reactivate_clients(
            server,
            ca_cert,
            previous_target=previous_target,
            allow_server_fallback=bool(server),
            profile=explicit_profile,
        )
    except (SystemExit, Exception):
        # Compensation already restored the pre-rollback whole-package
        # state and printed the underlying error.
        print(
            f"Error: 回滚后客户端重激活失败；已恢复到回滚前状态 {rollback_from}，"
            f"请检查后重试 gms-agent rollback {version}",
            file=sys.stderr,
        )
        return 1
    if reactivated:
        print(f"Re-activated from {version}: {', '.join(reactivated)}")
    return 0


# ---------------------------------------------------------------------------
# enroll
# ---------------------------------------------------------------------------

def write_enrollment_token(
    token: str, profile: str | None = None, client: str | None = None,
) -> list[str]:
    """Persist an enrolled Agent Service Token (0600); returns written paths.

    Resolution contract (multi-Controller fail-closed):

    - explicit ``profile`` → exactly that profile's token file;
    - else one distinct Controller across ALL installed profiles → write
      to every profile pointing at it (enumerated from the TOML store,
      never re-derived from the client, so a hand-named profile such as
      codex-prod gets the token);
    - else (zero or ambiguous) → write nothing and return [] — the caller
      must not guess which Controller a one-shot code belongs to.

    cmd_enroll used to derive the profile from the legacy
    <client>.env store only, so a TOML-only host crashed with
    FileNotFoundError AFTER the one-shot code had already been redeemed
    server-side — the token was lost with the code. Profile resolution now
    goes through the TOML store only (gms_agent.profile_store); the legacy
    env store no longer exists, so a TOML-only host cannot regress.
    """
    state_dir = profile_store.STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    written: list[str] = []
    if profile:
        flat = profile_store.load_profile(profile)
        token_value = flat.get("token_file", "")
        if not flat.get("profile") or not token_value:
            return []
        token_file = Path(token_value).expanduser()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        return [str(token_file)]
    unique_server, _ca = profile_server_and_ci_or_none()
    if not unique_server:
        return []
    for name in profile_store.list_profiles():
        flat = profile_store.load_profile(name)
        if profile_store.controller_url(flat) != unique_server:
            continue
        token_value = flat.get("token_file", "")
        if not token_value:
            continue
        token_file = Path(token_value).expanduser()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        written.append(str(token_file))
    return written


def cmd_enroll(args: argparse.Namespace) -> int:
    profile = (getattr(args, "profile", "") or "").strip()
    server, profile_ca = resolve_controller(getattr(args, "server", ""), profile)
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
        ca = (
            profile_ca
            if profile
            else os.environ.get("GMS_INSTALL_CA_CERT", "") or profile_ca
        )
        profile_insecure = (
            profile_store.load_profile(profile).get("insecure") == "true"
            if profile else os.environ.get("GMS_INSTALL_INSECURE") == "1"
        )
        if ca:
            context = ssl.create_default_context(cafile=ca)
        elif profile_insecure:
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
    # The token is written BEFORE any output so a later
    # failure cannot lose it. The exchange happens against the SAME
    # Controller the target profile(s) point at (resolve_controller above):
    # a one-shot code redeemed against Controller A is never stored into a
    # Controller B profile.
    try:
        written = write_enrollment_token(token, profile=profile)
    except OSError as error:
        print(f"Error: token 写盘失败（配对码已消费，请重新生成）: {error}", file=sys.stderr)
        return 8
    for path in written:
        print(f"Agent token enrolled (0600): {path}")
    if scopes:
        print(f"Scopes: {', '.join(scopes)}")
    if not written:
        eligible = " ".join(profile_store.list_profiles()) or "(none)"
        print(
            "Error: 本机没有指向唯一 Controller 的 eligible profile，"
            "token 未落盘（one-shot code 已消费，请重新生成）；"
            f"现安装的 profiles: {eligible}。请用 gms-agent install 配置 "
            "或用 --profile 指定目标。",
            file=sys.stderr,
        )
        return 8
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    """Validate package/profile/MCP state without exposing credentials."""

    report = doctor_report(
        getattr(args, "client", "auto"), getattr(args, "profile", "")
    )
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        versions = report["versions"]
        print(
            "Versions: "
            f"running={versions['running']} installed={versions['installed']} "
            f"current={versions['current_cli']} "
            f"consistent={str(versions['consistent']).lower()}"
        )
        for item in report["clients"]:
            selected = item["profile"]["selected"] or "(none/ambiguous)"
            print(
                f"  {item['client']}: profile={selected} "
                f"token={'ok' if item['token']['present'] and item['token']['mode_ok'] and item['token']['owner_ok'] else 'missing/invalid'} "
                f"mcp={'ok' if item['mcp']['registered'] else 'missing'} "
                f"skill={'ok' if item['skill_present'] else 'missing'}"
            )
        for action in report["actions"]:
            print(f"Action: {action}")
    return 0 if report["ok"] else 1


def cmd_profile(args: argparse.Namespace) -> int:
    """List, inspect, or activate an exact client profile."""

    action = getattr(args, "profile_action", "list")
    requested_client = getattr(args, "client", "")
    requested_name = getattr(args, "name", "")
    candidates = [profile_store.profile_path(name) for name in profile_store.list_profiles()]
    if requested_client:
        candidates = [
            path
            for path in candidates
            if load_profile(path.stem).get("client", "") == requested_client
        ]

    if action == "list":
        result = []
        for path in candidates:
            flat = load_profile(path.stem)
            result.append(
                {
                    "name": path.stem,
                    "client": flat.get("client", ""),
                    "controller": flat.get("url", ""),
                    "controller_url_valid": _controller_url_valid(flat.get("url", "")),
                    "token": _token_file_status(flat.get("token_file", "")),
                }
            )
        if getattr(args, "json", False):
            print(json.dumps({"profiles": result}, ensure_ascii=False, indent=2))
        else:
            for item in result:
                print(
                    f"{item['name']} | {item['client']} | {item['controller']} | "
                    f"token={'present' if item['token']['present'] else 'missing'}"
                )
        return 0

    if not requested_name or not re.fullmatch(r"[A-Za-z0-9_.-]+", requested_name):
        print("Error: a valid profile name is required", file=sys.stderr)
        return 2
    path = profile_store.profile_path(requested_name)
    if not path.is_file():
        print(f"Error: profile not found: {requested_name}", file=sys.stderr)
        return 2
    flat = load_profile(requested_name)
    profile_client = flat.get("client", "")
    if profile_client not in CLIENTS:
        print(f"Error: invalid profile client: {profile_client or '(missing)'}", file=sys.stderr)
        return 2
    if requested_client and requested_client != profile_client:
        print(
            f"Error: profile {requested_name} belongs to {profile_client}, "
            f"not {requested_client}",
            file=sys.stderr,
        )
        return 2
    result = {
        "name": requested_name,
        "client": profile_client,
        "controller": flat.get("url", ""),
        "controller_url_valid": _controller_url_valid(flat.get("url", "")),
        "ca_cert": flat.get("ca_cert", ""),
        "token": _token_file_status(flat.get("token_file", "")),
    }
    if action == "show":
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"{requested_name} | {profile_client} | {result['controller']} | "
                f"token={'present' if result['token']['present'] else 'missing'}"
            )
        return 0
    if action != "use":
        print(f"Error: unsupported profile action: {action}", file=sys.stderr)
        return 2
    if not result["controller_url_valid"]:
        print("Error: profile Controller URL is invalid", file=sys.stderr)
        return 2
    if not CURRENT_LINK.exists():
        print("Error: no installed runtime; run gms-agent install first", file=sys.stderr)
        return 2
    install_skill(profile_client, CURRENT_LINK)
    if profile_client == "kkagent":
        install_plugin_for_kkagent(CURRENT_LINK)
    else:
        reconcile_mcp(
            profile_client,
            str(result["controller"]),
            requested_name,
            str(result["ca_cert"]),
        )
    print(f"Activated profile {requested_name} for {profile_client}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        print(json.dumps(doctor_report(), ensure_ascii=False, indent=2))
        return 0
    print(f"Runtime:      {runtime_version()} (installed: {installed_version() or 'none'})")
    print(f"Runtime root: {RUNTIME_ROOT}")
    print(f"Detected:     {', '.join(detect_clients()) or '(none)'}")
    for client in configured_clients():
        # Report the EXACT ambiguity instead of silently displaying the
        # first sorted profile's token state (first-item display used to
        # mask a multi-Controller host as "configured + token OK").
        candidates = profile_store.profile_candidates(client)
        if len(candidates) == 1:
            flat = load_profile(candidates[0].stem)
            token_path = flat.get("token_file", "")
            token_ref = ""
            if token_path:
                ref = Path(token_path)
                token_ref = " + token" if ref.is_file() else " + token MISSING"
            state = "configured"
        elif len(candidates) > 1:
            state = f"AMBIGUOUS ({len(candidates)} profiles: "
            state += ", ".join(path.stem for path in candidates) + ")"
            token_ref = " — use `gms-agent profile use <NAME>` to disambiguate"
        else:
            state = "unconfigured"
            token_ref = ""
        print(f"  {client}: {state}{token_ref}")
    return 0
