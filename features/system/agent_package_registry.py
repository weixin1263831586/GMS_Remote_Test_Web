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


async def agent_package_manifest(request: Request):
    """GET /api/agent/packages/gms-remote-test/manifest"""
    version = _package_version()
    if not version or not AGENT_PACKAGE_DIR.is_dir():
        return error_response("agent package payload 未生成", status_code=404)
    try:
        archive = _build_archive(version)
    except FileNotFoundError as error:
        logger.error("agent package build failed: %s", error)
        return error_response(f"agent package 构建失败: {error}", status_code=500)
    sha256_hex = hashlib.sha256(archive).hexdigest()
    return {
        "success": True,
        "name": "gms-remote-test",
        "version": version,
        "signature": _manifest_signature(version, sha256_hex, len(archive)),
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
        archive = _build_archive(version)
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
    /api/system/skills/install.sh applies.
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
    from features.system.skill_archive_signing import skill_verify_key_b64

    content = content.replace(
        "__GMS_AGENT_VERIFY_KEY_B64__", skill_verify_key_b64()
    )
    return Response(
        content=content,
        media_type="text/x-python",
        headers={
            "Content-Disposition": 'inline; filename="gms-agent"',
            "Cache-Control": "no-store",
        },
    )
