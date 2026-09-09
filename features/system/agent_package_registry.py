"""Agent Package Registry endpoints (10.txt §十三, Phase 3).

The Controller becomes the single production distribution source for the
GMS Agent Runtime. The GitHub repo stays the development source; enterprise
build servers only ever need:

    curl --cacert ca.pem https://CONTROLLER:5001/api/agent/install -o install
    bash install

and later, from the installed runtime:

    gms-agent update        # manifest -> compare -> download -> verify ->
                            # atomic versions/<ver>/ install + current flip
    gms-agent rollback <ver>

Serving model: the registry reads the repository's agent package declaration
(agent/gms-remote-test/package.yaml) and the generated plugin payload
(plugins/gms-remote-test/), builds the distribution zip on demand (same
layout as tools/build_agent_package.py) and returns it with a strict
X-GMS-SHA256 integrity header, mirroring /api/system/skills.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from foundation.responses import error_response


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_PACKAGE_DIR = PROJECT_ROOT / "plugins" / "gms-remote-test"
PACKAGE_YAML = PROJECT_ROOT / "agent" / "gms-remote-test" / "package.yaml"

# Everything that goes into the distribution archive (synced release payload
# plus all three manifests). Paths are validated against this allowlist —
# never serve arbitrary files.
PAYLOAD_DIRS = ("scripts", "skills", "tests")
MANIFESTS = ("kk.plugin.json", "kimi.plugin.json", ".codex-plugin/plugin.json")


def _package_version() -> str:
    try:
        for line in PACKAGE_YAML.read_text(encoding="utf-8").splitlines():
            if line.startswith("version: "):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _payload_files() -> list[tuple[Path, str]]:
    """(absolute path, archive name) for every payload file, allowlisted."""
    files: list[tuple[Path, str]] = []
    for dirname in PAYLOAD_DIRS:
        base = AGENT_PACKAGE_DIR / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            files.append((path, f"gms-remote-test/{path.relative_to(AGENT_PACKAGE_DIR)}"))
    for manifest in MANIFESTS:
        path = AGENT_PACKAGE_DIR / manifest
        if path.is_file():
            files.append((path, f"gms-remote-test/{manifest}"))
    return files


def _build_archive(version: str) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in _payload_files():
            info = zipfile.ZipInfo(f"gms-remote-test-{version}/{arcname}")
            info.external_attr = 0o755 << 16 if path.suffix == ".sh" else 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _artifact_url(request: Request, version: str) -> str:
    return str(request.base_url).rstrip("/") + f"/api/agent/packages/gms-remote-test/{version}"


async def agent_package_manifest(request: Request):
    """GET /api/agent/packages/gms-remote-test/manifest"""
    version = _package_version()
    if not version or not AGENT_PACKAGE_DIR.is_dir():
        return error_response("agent package payload 未生成", status_code=404)
    archive = _build_archive(version)
    return {
        "success": True,
        "name": "gms-remote-test",
        "version": version,
        "artifacts": {
            "universal": {
                "url": _artifact_url(request, version),
                "sha256": hashlib.sha256(archive).hexdigest(),
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
    archive = _build_archive(version)
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

    Serves skills/gms-remote-test/scripts/gms-agent with the Controller URL
    embedded, so a fresh build server only needs:

        curl --cacert ca.pem https://CONTROLLER:5001/api/agent/install -o gms-agent
        python3 gms-agent install --server https://CONTROLLER:5001

    The installer script itself is read from the tracked skill source; the
    same base-URL validation as /api/system/skills/install.sh applies.
    """
    from urllib.parse import urlsplit

    installer_path = PROJECT_ROOT / "skills" / "gms-remote-test" / "scripts" / "gms-agent"
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
    return Response(
        content=content,
        media_type="text/x-python",
        headers={
            "Content-Disposition": 'inline; filename="gms-agent"',
            "Cache-Control": "no-store",
        },
    )
