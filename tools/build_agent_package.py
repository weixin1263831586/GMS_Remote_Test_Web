#!/usr/bin/env python3
"""Build distributable agent packages from the generated plugin payload.

The repository keeps one source of truth (skills/gms-remote-test/), one
generated release payload (plugins/gms-remote-test/, produced by
sync_package.sh) and one declared version (agent/gms-remote-test/package.yaml).
This tool turns the payload into per-client distribution archives:

  dist/gms-remote-test/<version>/universal/gms-remote-test-<version>.zip
      (skill + scripts + all three manifests — the Controller registry
      serves this archive)
  dist/gms-remote-test/<version>/kimi/…zip      (manifests: kimi only)
  dist/gms-remote-test/<version>/codex/…zip     (manifests: .codex-plugin)
  dist/gms-remote-test/<version>/kkagent/…zip   (manifests: kk only)

Every archive carries a SHA-256; tools/build_agent_package.py --print-manifest
emits the JSON manifest that the Controller Agent Package Registry embeds in
its manifest endpoint (10.txt §十三).

Usage:
    python tools/build_agent_package.py [--out dist]
    python tools/build_agent_package.py --print-manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "gms-remote-test"
PACKAGE_YAML = REPO_ROOT / "agent" / "gms-remote-test" / "package.yaml"

# Payload contents: everything synced by sync_package.sh plus the manifests.
PAYLOAD_DIRS = ["scripts", "skills", "tests"]
CLIENT_MANIFESTS = {
    "universal": ["kk.plugin.json", "kimi.plugin.json", ".codex-plugin/plugin.json"],
    "kimi": ["kimi.plugin.json"],
    "codex": [".codex-plugin/plugin.json"],
    "kkagent": ["kk.plugin.json"],
}


def read_version() -> str:
    for line in PACKAGE_YAML.read_text(encoding="utf-8").splitlines():
        if line.startswith("version: "):
            return line.split(":", 1)[1].strip()
    raise SystemExit(f"Error: no version in {PACKAGE_YAML}")


def _add_file(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
    info = zipfile.ZipInfo(arcname)
    info.external_attr = 0o755 << 16 if path.suffix in {".sh"} or not path.suffix else 0o644 << 16
    archive.writestr(info, path.read_bytes())


def build_zip(client: str, version: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"gms-remote-test-{version}-{client}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirname in PAYLOAD_DIRS:
            base = PLUGIN_DIR / dirname
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    _add_file(archive, path, f"gms-remote-test/{path.relative_to(PLUGIN_DIR)}")
        for manifest in CLIENT_MANIFESTS[client]:
            path = PLUGIN_DIR / manifest
            if not path.is_file():
                print(f"Error: missing manifest {manifest} for {client}", file=sys.stderr)
                raise SystemExit(1)
            _add_file(archive, path, f"gms-remote-test/{manifest}")
    return zip_path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist", help="output directory (default dist/)")
    parser.add_argument(
        "--print-manifest", action="store_true", help="print the registry manifest JSON"
    )
    args = parser.parse_args()

    version = read_version()
    out_root = (REPO_ROOT / args.out / "gms-remote-test" / version).resolve()
    manifest: dict[str, object] = {
        "name": "gms-remote-test",
        "version": version,
        "clients": sorted(CLIENT_MANIFESTS),
    }
    artifacts = {}
    for client in ("universal", "kimi", "codex", "kkagent"):
        zip_path = build_zip(client, version, out_root / client)
        artifacts[client] = {
            "path": str(zip_path.relative_to(REPO_ROOT)),
            "sha256": sha256_of(zip_path),
            "size": zip_path.stat().st_size,
        }
    manifest["artifacts"] = artifacts

    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if args.print_manifest:
        print(json.dumps(manifest, indent=2))
    else:
        for client, artifact in artifacts.items():
            print(f"  {client}: {artifact['path']} (sha256 {artifact['sha256'][:16]}...)")
        print(f"Manifest written: {manifest_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
