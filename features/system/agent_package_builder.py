"""Canonical agent-package archive builder — the single layout contract.

Both consumers MUST build the distribution archive through this module so
the served bytes and the released artifact can never drift apart
("ZIP 构建只能有一个实现"):

  * features/system/agent_package_registry.py (Controller registry)
  * tools/build_agent_package.py (release builder)

Layout contract — a SINGLE package root, identical for every client
variant (only the bundled manifests differ):

    gms-remote-test/
      scripts/                     gms-remote-test.sh, mcp_server.py, gms-agent, gms_agent/, ...
      skills/gms-remote-test/      SKILL.md, references/, agents/
      tests/
      kk.plugin.json
      kimi.plugin.json
      .codex-plugin/plugin.json    (universal only)

`gms-agent` locates the package root at exactly this single root after
extraction; nothing may wrap it in a second version-named directory.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path


PACKAGE_ROOT_NAME = "gms-remote-test"
PAYLOAD_DIRS = ("scripts", "skills", "tests", "docs")
CLIENT_MANIFESTS: dict[str, list[str]] = {
    "universal": ["kk.plugin.json", "kimi.plugin.json", ".codex-plugin/plugin.json"],
    "kimi": ["kimi.plugin.json"],
    "codex": [".codex-plugin/plugin.json"],
    "kkagent": ["kk.plugin.json"],
}


def payload_files(plugin_dir: Path, client: str = "universal") -> list[tuple[Path, str]]:
    """(absolute path, archive name) for every payload file, allowlisted.

    Never serves arbitrary files: only PAYLOAD_DIRS subtrees plus the
    client's manifests, flattened under the single ``gms-remote-test/``
    archive root.
    """
    if client not in CLIENT_MANIFESTS:
        raise ValueError(f"unknown package client variant: {client!r}")
    files: list[tuple[Path, str]] = []
    for dirname in PAYLOAD_DIRS:
        base = plugin_dir / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            files.append((path, f"{PACKAGE_ROOT_NAME}/{path.relative_to(plugin_dir)}"))
    for manifest in CLIENT_MANIFESTS[client]:
        path = plugin_dir / manifest
        if path.is_file():
            files.append((path, f"{PACKAGE_ROOT_NAME}/{manifest}"))
    return files


def _archive_mode(path: Path) -> int:
    # Executable bit: shell scripts, extension-less executables (gms-agent)
    # and any file carrying a POSIX shebang (mcp_launcher.py is executed
    # DIRECTLY as "./scripts/mcp_launcher.py" by the plugin manifests).
    # Everything else 0644.
    if path.suffix == ".sh" or not path.suffix:
        return 0o755 << 16
    try:
        with path.open("rb") as handle:
            if handle.read(2) == b"#!":
                return 0o755 << 16
    except OSError:
        pass
    return 0o644 << 16


def check_client_manifests(plugin_dir: Path, client: str = "universal") -> None:
    """Raise if a client variant is missing its required manifests."""
    missing = [
        manifest
        for manifest in CLIENT_MANIFESTS[client]
        if not (plugin_dir / manifest).is_file()
    ]
    if missing:
        raise FileNotFoundError(f"missing manifest(s) for {client}: {missing}")


def build_package_bytes(plugin_dir: Path, version: str, client: str = "universal") -> bytes:
    """Build the distribution zip as bytes with the single-root layout."""
    if not version:
        raise ValueError("package version must be a non-empty string")
    check_client_manifests(plugin_dir, client)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in payload_files(plugin_dir, client):
            info = zipfile.ZipInfo(arcname)
            info.external_attr = _archive_mode(path)
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


__all__ = [
    "CLIENT_MANIFESTS",
    "PACKAGE_ROOT_NAME",
    "PAYLOAD_DIRS",
    "build_package_bytes",
    "check_client_manifests",
    "payload_files",
]
