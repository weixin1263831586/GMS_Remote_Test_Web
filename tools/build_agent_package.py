#!/usr/bin/env python3
"""Build distributable agent packages from the generated plugin payload.

The repository keeps one source of truth (agent/gms-remote-test/, synced
into the generated plugins/gms-remote-test/ by
tools/sync_agent_package.py) and one declared version
(agent/gms-remote-test/package.yaml).
This tool turns the payload into per-client distribution archives:

  dist/gms-remote-test/<version>/universal/gms-remote-test-<version>.zip
      (skill + scripts + all three manifests — the Controller registry
      serves this archive)
  dist/gms-remote-test/<version>/kimi/…zip      (manifests: kimi only)
  dist/gms-remote-test/<version>/codex/…zip     (manifests: .codex-plugin)
  dist/gms-remote-test/<version>/kkagent/…zip   (manifests: kk only)

Archive construction is DELEGATED to the canonical builder
(features/system/agent_package_builder.py) — the exact same code path the
Controller registry serves from — so the released zip and the served zip
are byte-identical by construction.

Every archive carries a SHA-256; --print-manifest emits the JSON manifest
that the Controller Agent Package Registry embeds in its manifest
endpoint.

Usage:
    python tools/build_agent_package.py [--out dist]
    python tools/build_agent_package.py --print-manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "gms-remote-test"
PACKAGE_YAML = REPO_ROOT / "agent" / "gms-remote-test" / "package.yaml"

sys.path.insert(0, str(REPO_ROOT))
from features.system.agent_package_builder import CLIENT_MANIFESTS, build_package_bytes  # noqa: E402


# The canonical generated plugin payload (synced from
# agent/gms-remote-test by tools/sync_agent_package.py) is the packaging
# input — builder and registry share the exact same tree.


def read_version() -> str:
    for line in PACKAGE_YAML.read_text(encoding="utf-8").splitlines():
        if line.startswith("version: "):
            return line.split(":", 1)[1].strip()
    raise SystemExit(f"Error: no version in {PACKAGE_YAML}")


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
    for client in sorted(CLIENT_MANIFESTS):
        try:
            data = build_package_bytes(PLUGIN_DIR, version, client=client)
        except FileNotFoundError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        out_dir = out_root / client
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / f"gms-remote-test-{version}-{client}.zip"
        zip_path.write_bytes(data)
        artifacts[client] = {
            "path": str(zip_path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    manifest["artifacts"] = artifacts

    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if args.print_manifest:
        print(json.dumps(manifest, indent=2))
    else:
        for client, artifact in artifacts.items():
            print(f"  {client}: {artifact['path']} (sha256 {artifact['sha256'][:16]}...)")
        print(f"Manifest written: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
