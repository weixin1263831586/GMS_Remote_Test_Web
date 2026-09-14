"""Agent Package Registry endpoints (Phase 3).

The Controller becomes the single production distribution source for the
GMS Agent Runtime. The GitHub repo stays the development source; enterprise
build servers only ever need:

    curl --cacert ca.pem https://CONTROLLER:5001/api/agent/install -o gms-agent
    python3 gms-agent install --server https://CONTROLLER:5001

and later, from the installed runtime:

    gms-agent update        # manifest -> compare -> download -> verify ->
                            # atomic versions/<ver>/ install + whole-package
                            # re-activation + current flip
    gms-agent rollback <ver>

Serving model: the registry reads the repository's agent package declaration
(agent/gms-remote-test/package.yaml) and the generated plugin payload
(plugins/gms-remote-test/). Archive construction goes through the ONE
canonical builder (features/system/agent_package_builder.py — the same
module tools/build_agent_package.py uses), so the served zip and the
released artifact share a single layout contract: a single
``gms-remote-test/`` root, never double-wrapped.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from features.system.agent_package_builder import build_package_bytes
from foundation.responses import error_response


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_PACKAGE_DIR = PROJECT_ROOT / "plugins" / "gms-remote-test"
PACKAGE_YAML = PROJECT_ROOT / "agent" / "gms-remote-test" / "package.yaml"


def _package_version() -> str:
    try:
        for line in PACKAGE_YAML.read_text(encoding="utf-8").splitlines():
            if line.startswith("version: "):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _build_archive(version: str) -> bytes:
    """Serve exactly what the release builder would have produced."""
    return build_package_bytes(AGENT_PACKAGE_DIR, version, client="universal")


# Same-version content immutability: without this cache the
# registry rebuilds the zip from the live working tree on EVERY request, so
# the same version string could serve different bytes after an edit (and
# installed hosts that already see that version never re-download). The
# FIRST build after process start pins (sha256, archive) for that version;
# any later rebuild whose sha256 differs is refused (500) instead of being
# served — same version, same bytes, for every client, forever.
_IMMUTABLE_CACHE: dict[str, tuple[str, bytes]] = {}


def _immutable_archive(version: str) -> bytes:
    cached = _IMMUTABLE_CACHE.get(version)
    if cached is not None:
        pinned_sha, pinned_archive = cached
        fresh = build_package_bytes(AGENT_PACKAGE_DIR, version, client="universal")
        fresh_sha = hashlib.sha256(fresh).hexdigest()
        if fresh_sha != pinned_sha:
            logger.error(
                "agent package drift: version %s already published as %s but "
                "working tree now builds to %s; bump the version and re-sync",
                version, pinned_sha[:16], fresh_sha[:16],
            )
            raise DriftedVersionError(version, pinned_sha, fresh_sha)
        return pinned_archive
    archive = build_package_bytes(AGENT_PACKAGE_DIR, version, client="universal")
    _IMMUTABLE_CACHE[version] = (hashlib.sha256(archive).hexdigest(), archive)
    return archive


class DriftedVersionError(RuntimeError):
    """A version was rebuilt with different content than the pinned serve."""

    def __init__(self, version: str, pinned_sha: str, fresh_sha: str):
        super().__init__(
            f"version {version} content drifted (pinned {pinned_sha[:16]}, "
            f"rebuild {fresh_sha[:16]})"
        )
        self.version = version
        self.pinned_sha = pinned_sha
        self.fresh_sha = fresh_sha


def _artifact_url(request: Request, version: str) -> str:
    return str(request.base_url).rstrip("/") + f"/api/agent/packages/gms-remote-test/{version}"


def _manifest_signature(version: str, sha256_hex: str, size: int) -> str:
    """Optional Ed25519 manifest signature (anti-tamper binding).

    Signed payload = "name|version|sha256|size" — the fields a tamperer
    would need to swap. Returns "" when no signing key is configured
    (signature becomes optional; clients pinning a verify key still reject
    unsigned manifests, fail-closed).
    """
    from features.system.skill_archive_signing import sign_skill_archive

    payload = f"gms-remote-test|{version}|{sha256_hex}|{size}".encode()
    return sign_skill_archive(payload)


def _unsigned_in_production() -> bool:
    """Never serve a silent SHA-only fallback in production.

    Startup validation (bootstrap.production_security) already fails fast
    without a signing key; this endpoint-level guard covers a key removed
    or becoming unreadable at runtime. Development keeps SHA-only serving
    (signing stays opt-in until first production enrollment).
    """
    from foundation.runtime_settings import is_production_environment

    return is_production_environment()


async def agent_package_manifest(request: Request):
    """GET /api/agent/packages/gms-remote-test/manifest"""
    version = _package_version()
    if not version or not AGENT_PACKAGE_DIR.is_dir():
        return error_response("agent package payload 未生成", status_code=404)
    try:
        archive = _immutable_archive(version)
    except DriftedVersionError as error:
        logger.error("agent package build failed: %s", error)
        return error_response(str(error), status_code=500)
    except FileNotFoundError as error:
        logger.error("agent package build failed: %s", error)
        return error_response(f"agent package 构建失败: {error}", status_code=500)
    sha256_hex = hashlib.sha256(archive).hexdigest()
    signature = _manifest_signature(version, sha256_hex, len(archive))
    # Production refuses to publish an unsigned manifest —
    # the trust chain (pinned verify key → signature → artifact) must be
    # complete, not a silent SHA-only downgrade.
    if not signature and _unsigned_in_production():
        return error_response(
            "production 模式必须配置 GMS_SKILL_SIGNING_KEY_FILE 才能发布 agent package 清单",
            status_code=503,
        )
    return {
        "success": True,
        "name": "gms-remote-test",
        "version": version,
        "signature": signature,
        "artifacts": {
            "universal": {
                "url": _artifact_url(request, version),
                "sha256": sha256_hex,
                "size": len(archive),
            }
        },
    }


async def agent_package_download(version: str, request: Request):
    """GET /api/agent/packages/gms-remote-test/{version}"""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version or ""):
        return error_response("非法版本号", status_code=400)
    declared = _package_version()
    if not declared or version != declared:
        return error_response(
            f"版本 {version} 不存在（当前发布版本: {declared or '未知'}）", status_code=404
        )
    try:
        archive = _immutable_archive(version)
    except DriftedVersionError as error:
        return error_response(str(error), status_code=500)
    except FileNotFoundError as error:
        return error_response(f"agent package 构建失败: {error}", status_code=500)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="gms-remote-test-{version}.zip"',
            "X-GMS-SHA256": hashlib.sha256(archive).hexdigest(),
            "Cache-Control": "no-store",
        },
    )


async def agent_bootstrap_installer(request: Request):
    """GET /api/agent/install — the single one-command bootstrap.

    Serves agent/gms-remote-test/runtime/gms-agent with the Controller URL
    embedded. The downloaded script is STANDALONE (no package around it):
    `install` detects that and bootstraps by downloading the package from
    the registry (gms-agent fetch_registry_package) — a two-layer bootstrap
    lifecycle: standalone script first, full package on top of it. The same
    base-URL validation as /api/agent/install.sh applies.
    """
    from urllib.parse import urlsplit

    installer_path = (
        PROJECT_ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-agent"
    )
    if not installer_path.is_file():
        return error_response("agent installer 不可用", status_code=500)
    server_url = str(request.base_url).rstrip("/")
    parsed = urlsplit(server_url)
    base_host = parsed.hostname or ""
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or not re.fullmatch(r"[A-Za-z0-9.\-_:]+", base_host)
        or not (parsed.port is None or 0 < parsed.port < 65536)
    ):
        return error_response("无法从当前请求确定有效的服务地址", status_code=400)
    content = installer_path.read_text(encoding="utf-8").replace(
        "__GMS_AGENT_DEFAULT_SERVER__", server_url
    )
    # Pin the release Ed25519 public key into the downloaded bootstrap so
    # its very first registry download verifies the manifest signature
    # (no trust-on-first-use gap). Empty when signing disabled.
    # Production never hands out a SHA-only bootstrap —
    # without the pinned verify key the first download cannot verify.
    from features.system.skill_archive_signing import skill_verify_key_b64
    from foundation.runtime_settings import is_production_environment

    verify_key = skill_verify_key_b64()
    if not verify_key and is_production_environment():
        return error_response(
            "production 模式必须配置 GMS_SKILL_SIGNING_KEY_FILE 才能分发 agent installer",
            status_code=503,
        )
    content = content.replace("__GMS_AGENT_VERIFY_KEY_B64__", verify_key)
    return Response(
        content=content,
        media_type="text/x-python",
        headers={
            "Content-Disposition": 'inline; filename="gms-agent"',
            "Cache-Control": "no-store",
        },
    )

# ---------------------------------------------------------------------------
# One-line installer (GET /api/agent/install.sh)
# ---------------------------------------------------------------------------

_INSTALL_SH_TEMPLATE = r'''#!/usr/bin/env bash
# GMS Remote Test agent runtime — one-line installer.
#
# Usage:
#   curl -k -fsSL __SERVER_URL__/api/agent/install.sh | bash -s -- --paircode <配对码> [options...]
#
#   --paircode <配对码>   Controller Web UI 铸的一次性配对码（推荐写法;
#                         --pairing-code / --enroll-code 等价; 兼容旧的
#                         第一个位置参数写法,但命名形式更明确）。
#
#   自签名部署的"第一接触": 拉取本脚本这一次无法做服务器校验(还没有 CA),
#   用 -k 获取脚本本身(可先人工核对脚本内容); 脚本随后从
#   /api/agent/ca.crt TOFU 获取 Controller CA 并落盘到
#   ~/.local/state/gms-remote-test/controller-ca.pem, 之后的 bootstrap/
#   manifest/包下载全部走严格 TLS + SHA-256 + Ed25519 签名校验。
#   更严格的替代: 带外分发 CA 后 export GMS_INSTALL_CA_CERT=/path/ca.pem,
#   并用 curl --cacert 拉取本脚本(全程严格校验,无 TOFU)。
#   (受控实验环境可用 GMS_INSTALL_ALLOW_INSECURE=1 显式降级,生产端点拒绝)。
#
#   CODE  (optional) one-shot enrollment code from the Controller web UI;
#         when given, install exchanges it for a 0600 Agent Service Token
#         in the same run (no separate `gms-agent enroll` step).
#   Remaining arguments are forwarded to `gms-agent install`
#   (e.g. --client kkagent / codex / kimi / auto).
#
# After installing: restart the agent client (kkagent / codex / kimi) so the
# gms-* tools load. Later lifecycle:
#   ~/.local/share/gms-remote-test/current/scripts/gms-agent update|rollback <ver>
set -euo pipefail

SERVER='__SERVER_URL__'

PASS_ARGS=()
CODE=""
while (( $# )); do
  case "$1" in
    # 选项与其值一起透传（值不能被误认成配对码）。
    --client|--package|--server)
      if (( $# < 2 )); then
        echo "Error: $1 需要一个参数" >&2
        exit 2
      fi
      PASS_ARGS+=("$1" "$2")
      shift 2
      ;;
    # 配对码的显式命名形式（推荐）；--enroll-code 为既有别名。
    --enroll-code|--paircode|--pairing-code)
      if (( $# < 2 )); then
        echo "Error: $1 需要一个参数" >&2
        exit 2
      fi
      CODE="$2"
      shift 2
      ;;
    --*)
      PASS_ARGS+=("$1")
      shift
      ;;
    *)
      # 第一个裸位置参数 = 配对码（可选）。
      if [[ -z "$CODE" ]]; then
        CODE="$1"
      else
        PASS_ARGS+=("$1")
      fi
      shift
      ;;
  esac
done
# 配对码只经环境变量传给 gms-agent(不进 argv/ps/shell history)。
# gms-agent 端 GMS_AGENT_ENROLL_CODE 优先于 --enroll-code。
unset GMS_AGENT_ENROLL_CODE
if [[ -n "$CODE" ]]; then
  export GMS_AGENT_ENROLL_CODE="$CODE"
  CODE=""
fi

DOWNLOADER=""
if command -v curl >/dev/null 2>&1; then
  DOWNLOADER=curl
elif command -v wget >/dev/null 2>&1; then
  DOWNLOADER=wget
else
  echo "Error: 需要 curl 或 wget 之一" >&2
  exit 3
fi

# 传输层策略 —— TLS 校验 fail-closed:
#   1) GMS_INSTALL_CA_CERT 指向可读证书 → 显式信任私有 CA 严格校验(推荐);
#   2) GMS_INSTALL_ALLOW_INSECURE=1 且服务端允许(非生产) → 显式降级跳过
#      校验,仅限受控实验环境,并传导给 python 阶段;
#   3) 其余情况 → 使用系统信任链默认校验(自签名证书会失败,这是预期)。
# 服务端在 production 环境渲染 ALLOW_INSECURE=0,insecure 引导被直接拒绝。
ALLOW_INSECURE='__ALLOW_INSECURE__'
if [[ "${GMS_INSTALL_ALLOW_INSECURE:-0}" == "1" && "$ALLOW_INSECURE" != "1" ]]; then
  echo "Error: GMS_INSTALL_ALLOW_INSECURE=1 被生产环境 Controller 拒绝;请通过 GMS_INSTALL_CA_CERT 信任 Controller CA" >&2
  exit 4
fi
# python 阶段(bootstrap 拉 manifest/包)继承同一 TLS 策略: 显式/TOFU CA 传导
# GMS_INSTALL_CA_CERT; insecure 降级传导 GMS_INSTALL_INSECURE=1。

CA_STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test"
WORK_DIR="$(mktemp -d /tmp/gms-agent-install.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

fetch_bootstrap() {  # $1 = CA 路径("" = 系统信任链)
  case "$DOWNLOADER" in
    curl)
      if [[ -n "$1" ]]; then
        curl -fsSL --cacert "$1" "$SERVER/api/agent/install" -o "$WORK_DIR/gms-agent"
      else
        curl -fsSL "$SERVER/api/agent/install" -o "$WORK_DIR/gms-agent"
      fi
      ;;
    wget)
      if [[ -n "$1" ]]; then
        wget --ca-certificate="$1" -qO "$WORK_DIR/gms-agent" "$SERVER/api/agent/install"
      else
        wget -qO "$WORK_DIR/gms-agent" "$SERVER/api/agent/install"
      fi
      ;;
  esac
}

echo "Fetching gms-agent bootstrap from $SERVER ..."
if [[ "${GMS_INSTALL_ALLOW_INSECURE:-0}" == "1" ]]; then
  # 显式降级(仅非生产 Controller 渲染允许):跳过校验并传导给 python 阶段。
  export GMS_INSTALL_INSECURE=1
  echo "Warning: GMS_INSTALL_ALLOW_INSECURE=1,跳过 TLS 证书校验(仅限受控实验环境)" >&2
  case "$DOWNLOADER" in
    curl) curl -kfsSL "$SERVER/api/agent/install" -o "$WORK_DIR/gms-agent" ;;
    wget) wget --no-check-certificate -qO "$WORK_DIR/gms-agent" "$SERVER/api/agent/install" ;;
  esac
elif [[ -n "${GMS_INSTALL_CA_CERT:-}" ]]; then
  if [[ ! -r "$GMS_INSTALL_CA_CERT" ]] || ! fetch_bootstrap "$GMS_INSTALL_CA_CERT"; then
    echo "Error: 无法用 GMS_INSTALL_CA_CERT=$GMS_INSTALL_CA_CERT 完成下载(TLS 校验失败或网络不通);请确认该文件是当前 Controller 的 CA" >&2
    exit 5
  fi
elif fetch_bootstrap ""; then
  : # 系统信任链严格校验成功
else
  # 自签名部署的预期路径: TOFU 从 Controller 拉 CA 后严格重试。
  # 内容完整性不依赖此信任: manifest 另有 SHA-256 + Ed25519 签名校验。
  echo "系统信任链无法校验 Controller 证书,尝试 TOFU: $SERVER/api/agent/ca.crt ..." >&2
  case "$DOWNLOADER" in
    curl) curl -kfsSL "$SERVER/api/agent/ca.crt" -o "$WORK_DIR/controller-ca.pem" ;;
    wget) wget --no-check-certificate -qO "$WORK_DIR/controller-ca.pem" "$SERVER/api/agent/ca.crt" ;;
  esac
  if [[ ! -s "$WORK_DIR/controller-ca.pem" ]] || ! grep -q "BEGIN CERTIFICATE" "$WORK_DIR/controller-ca.pem"; then
    echo "Error: 无法获取 Controller CA。请人工核对证书，并设置 GMS_INSTALL_CA_CERT=/path/to/controller-ca.pem 后重试。" >&2
    exit 5
  fi
  # 先落盘到持久路径再导出: profile 会记录这个 ca_cert 路径,
  # 指向 WORK_DIR 会被 EXIT trap 清掉变成悬空引用。
  PERSISTENT_CA="$CA_STATE_DIR/controller-ca.pem"
  if mkdir -p "$CA_STATE_DIR" 2>/dev/null && cp "$WORK_DIR/controller-ca.pem" "$PERSISTENT_CA" 2>/dev/null; then
    chmod 700 "$CA_STATE_DIR" 2>/dev/null || true
    chmod 644 "$PERSISTENT_CA" 2>/dev/null || true
    echo "Controller CA 已保存: $PERSISTENT_CA (可用作 GMS_INSTALL_CA_CERT / GMS_CURL_CA_CERT)" >&2
  else
    PERSISTENT_CA="$WORK_DIR/controller-ca.pem"
    echo "Warning: CA 无法落盘到 $CA_STATE_DIR, 仅本次安装生效" >&2
  fi
  export GMS_INSTALL_CA_CERT="$PERSISTENT_CA"
  if ! fetch_bootstrap "$PERSISTENT_CA"; then
    echo "Error: 使用 TOFU CA 仍无法完成下载;请人工核对 Controller 证书后重试" >&2
    exit 5
  fi
  echo "TLS: 已用 TOFU 获取的 Controller CA 完成严格校验。" >&2
fi

python3 "$WORK_DIR/gms-agent" install --server "$SERVER" \
  "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"
'''


async def agent_install_sh(request: Request):
    """GET /api/agent/install.sh — one-line `curl | bash` installer.

    Renders a thin bash wrapper bound to this request's base URL: it fetches
    the gms-agent bootstrap (GET /api/agent/install) and runs `install`; a
    enrollment code (when given) is exchanged in the same run, so install +
    token provisioning happen in a single command:

        curl -k -fsSL https://CONTROLLER:5001/api/agent/install.sh | bash -s -- --paircode <CODE>

    (--paircode / --pairing-code / --enroll-code are equivalent; a bare
    first positional argument is still accepted for compatibility.)

    TLS is fail-closed: the rendered script verifies certificates by default
    (system trust store, or GMS_INSTALL_CA_CERT). On a fresh host without a
    provisioned CA — the self-signed deployment case — it falls back to
    TOFU: it fetches GET /api/agent/ca.crt once, stores it under
    ~/.local/state/gms-remote-test/ and retries with strict verification.
    Insecure bootstrap via GMS_INSTALL_ALLOW_INSECURE=1 only works while the
    Controller runs in a non-production environment; production renders
    ALLOW_INSECURE=0 and the script rejects the downgrade outright.
    """
    from urllib.parse import urlsplit

    from foundation.runtime_settings import is_production_environment

    server_url = str(request.base_url).rstrip("/")
    parsed = urlsplit(server_url)
    base_host = parsed.hostname or ""
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or not re.fullmatch(r"[A-Za-z0-9.\-_:]+", base_host)
        or not (parsed.port is None or 0 < parsed.port < 65536)
    ):
        logger.warning("[AGENT_INSTALL_SH] rejected suspicious base_url: %r", server_url)
        return error_response("无法从当前请求确定有效的服务地址", status_code=400)
    allow_insecure = "0" if is_production_environment() else "1"
    content = (
        _INSTALL_SH_TEMPLATE.replace("__SERVER_URL__", server_url)
        .replace("__ALLOW_INSECURE__", allow_insecure)
    )
    return Response(
        content=content,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": 'inline; filename="install-gms-remote-test.sh"',
            "Cache-Control": "no-store",
        },
    )


async def agent_ca_cert(request: Request):
    """GET /api/agent/ca.crt — Controller CA 证书分发(TOFU 信任源)。

    one-line installer 在系统信任链校验失败时从这里拉取 CA,再以严格校验
    完成 install.sh 的自动回退。仅分发证书(公开物),绝不涉及私钥。
    解析顺序: GMS_CONTROLLER_CA_FILE > secrets/certs/gms-local-ca.crt
    (正规 CA 部署) > secrets/certs/gms-local.crt(旧叶子自签部署)。
    """
    import os

    from foundation.config_paths import certificates_path

    candidates: list[Path] = []
    env_file = os.environ.get("GMS_CONTROLLER_CA_FILE", "")
    if env_file:
        candidates.append(Path(env_file))
    cert_dir = Path(certificates_path(str(PROJECT_ROOT)))
    candidates.append(cert_dir / "gms-local-ca.crt")
    candidates.append(cert_dir / "gms-local.crt")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return Response(
                    content=candidate.read_bytes(),
                    media_type="application/x-pem-file",
                    headers={
                        "Content-Disposition": 'inline; filename="controller-ca.pem"',
                        "Cache-Control": "no-store",
                    },
                )
        except OSError:
            continue
    return error_response("Controller CA 证书不可用", status_code=404)
