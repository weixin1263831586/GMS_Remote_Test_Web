"""Agent Package Registry endpoints (10.txt §十三, Phase 3).

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


# 11.txt 审核 P1: same-version content immutability. Without this cache the
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
    """Optional Ed25519 manifest signature (10.txt §二十).

    Signed payload = "name|version|sha256|size" — the fields a tamperer
    would need to swap. Returns "" when no signing key is configured
    (signature becomes optional; clients pinning a verify key still reject
    unsigned manifests, fail-closed).
    """
    from features.system.skill_archive_signing import sign_skill_archive

    payload = f"gms-remote-test|{version}|{sha256_hex}|{size}".encode()
    return sign_skill_archive(payload)


def _unsigned_in_production() -> bool:
    """15.txt 审核 P2: never serve a silent SHA-only fallback in production.

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
    # 15.txt 审核 P2: production refuses to publish an unsigned manifest —
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
    """GET /api/agent/install — the single one-command bootstrap (10.txt §十三).

    Serves agent/gms-remote-test/runtime/gms-agent with the Controller URL
    embedded. The downloaded script is STANDALONE (no package around it):
    `install` detects that and bootstraps by downloading the package from
    the registry (gms-agent fetch_registry_package) — the two-layer
    bootstrap lifecycle from 10.txt §四. The same base-URL validation as
    /api/agent/install.sh applies.
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
    # (10.txt §二十: no trust-on-first-use gap). Empty when signing disabled.
    # 15.txt 审核 P2: production never hands out a SHA-only bootstrap —
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
#   curl -kfsSL __SERVER_URL__/api/agent/install.sh | bash -s -- [CODE] [options...]
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

CODE_ARGS=()
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
    --enroll-code)
      if (( $# < 2 )); then
        echo "Error: --enroll-code 需要一个参数" >&2
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
if [[ -n "$CODE" ]]; then
  CODE_ARGS=(--enroll-code "$CODE")
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

# 传输层策略: 配置了 GMS_INSTALL_CA_CERT 时严格校验; 否则(典型为自签名
# 内网部署)降级为不校验传输层并提示。内容完整性由 gms-agent 的清单
# SHA-256 + Ed25519 签名校验兜底; 有主动中间人风险的强信任环境应通过
# GMS_INSTALL_CA_CERT 下发 CA。
FETCH_TLS=()
case "$DOWNLOADER" in
  curl)
    if [[ -n "${GMS_INSTALL_CA_CERT:-}" && -r "${GMS_INSTALL_CA_CERT}" ]]; then
      FETCH_TLS=(-fsSL --cacert "$GMS_INSTALL_CA_CERT")
    else
      FETCH_TLS=(-kfsSL)
      # python 阶段(bootstrap 拉 manifest/包)继承同一 TLS 策略。
      export GMS_INSTALL_INSECURE=1
      echo "Warning: 未配置 GMS_INSTALL_CA_CERT,跳过 TLS 证书校验(自签名部署)" >&2
    fi
    ;;
  wget)
    if [[ -n "${GMS_INSTALL_CA_CERT:-}" && -r "${GMS_INSTALL_CA_CERT}" ]]; then
      FETCH_TLS=(--ca-certificate="$GMS_INSTALL_CA_CERT" -qO)
    else
      FETCH_TLS=(--no-check-certificate -qO)
      # python 阶段(bootstrap 拉 manifest/包)继承同一 TLS 策略。
      export GMS_INSTALL_INSECURE=1
      echo "Warning: 未配置 GMS_INSTALL_CA_CERT,跳过 TLS 证书校验(自签名部署)" >&2
    fi
    ;;
esac

WORK_DIR="$(mktemp -d /tmp/gms-agent-install.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Fetching gms-agent bootstrap from $SERVER ..."
case "$DOWNLOADER" in
  curl) curl "${FETCH_TLS[@]}" "$SERVER/api/agent/install" -o "$WORK_DIR/gms-agent" ;;
  wget) wget "${FETCH_TLS[@]}" "$SERVER/api/agent/install" -O "$WORK_DIR/gms-agent" ;;
esac

python3 "$WORK_DIR/gms-agent" install --server "$SERVER" \
  "${CODE_ARGS[@]+"${CODE_ARGS[@]}"}" \
  "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"
'''


async def agent_install_sh(request: Request):
    """GET /api/agent/install.sh — one-line `curl | bash` installer.

    Renders a thin bash wrapper bound to this request's base URL: it fetches
    the gms-agent bootstrap (GET /api/agent/install) and runs `install`; a
    positional enrollment code (when given) is forwarded as --enroll-code so
    install + token exchange happen in a single command:

        curl -kfsSL https://CONTROLLER:5001/api/agent/install.sh | bash -s -- <CODE>
    """
    from urllib.parse import urlsplit

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
    content = _INSTALL_SH_TEMPLATE.replace("__SERVER_URL__", server_url)
    return Response(
        content=content,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": 'inline; filename="install-gms-remote-test.sh"',
            "Cache-Control": "no-store",
        },
    )
