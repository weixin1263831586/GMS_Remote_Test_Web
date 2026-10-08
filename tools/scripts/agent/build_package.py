#!/usr/bin/env python3
"""Build distributable agent packages from the generated plugin payload.

The repository keeps one source of truth (agent/gms-remote-test/, synced
into the generated plugins/gms-remote-test/ by
tools/scripts/agent/sync_package.py) and one declared version
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
    python tools/scripts/agent/build_package.py [--out dist] [--keep 3]
    python tools/scripts/agent/build_package.py --print-manifest

After a successful build the ``--keep`` newest version directories under
``dist/gms-remote-test/`` are kept (the just-built version always survives);
older rebuildable version directories are pruned automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


REPO_ROOT = find_repo_root()
PLUGIN_DIR = REPO_ROOT / "plugins" / "gms-remote-test"
PACKAGE_YAML = REPO_ROOT / "agent" / "gms-remote-test" / "package.yaml"

sys.path.insert(0, str(REPO_ROOT))
from features.system.agent_package_builder import CLIENT_MANIFESTS, build_package_bytes  # noqa: E402


# The canonical generated plugin payload (synced from
# agent/gms-remote-test by tools/scripts/agent/sync_package.py) is the
# packaging input — builder and registry share the exact same tree.


def read_version() -> str:
    for line in PACKAGE_YAML.read_text(encoding="utf-8").splitlines():
        if line.startswith("version: "):
            return line.split(":", 1)[1].strip()
    raise SystemExit(f"Error: no version in {PACKAGE_YAML}")


# Old release archives are rebuildable from the generated plugin payload, so
# building a new version prunes stale ones instead of accumulating dist/
# forever. Only directories whose name parses as a plain dotted version are
# eligible; anything else under dist/gms-remote-test is left untouched.
DEFAULT_KEEP_VERSIONS = 3
VERSION_DIR_RE = re.compile(r"^\d+(\.\d+)*$")


def _version_sort_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in name.split("."))


def prune_old_versions(out_base: Path, current: str, keep: int) -> list[str]:
    """Delete stale version dirs, keeping the ``keep`` newest ones.

    The just-built version is always kept, even if fewer than ``keep``
    directories exist. Returns the pruned directory names.
    """
    if keep <= 0 or not out_base.is_dir():
        return []
    versions = [
        entry.name for entry in out_base.iterdir()
        if entry.is_dir() and VERSION_DIR_RE.fullmatch(entry.name)
    ]
    keep_versions = {current, *sorted(versions, key=_version_sort_key, reverse=True)[:keep]}
    pruned = []
    for name in sorted(versions, key=_version_sort_key):
        if name in keep_versions:
            continue
        shutil.rmtree(out_base / name)
        pruned.append(name)
    return pruned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist", help="output directory (default dist/)")
    parser.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP_VERSIONS, metavar="N",
        help=f"keep the N newest version directories after building; 0 keeps all (default {DEFAULT_KEEP_VERSIONS})",
    )
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

    for name in prune_old_versions(out_root.parent, version, args.keep):
        print(f"Pruned old version dir: {out_root.parent / name}")

    if args.print_manifest:
        print(json.dumps(manifest, indent=2))
    else:
        for client, artifact in artifacts.items():
            print(f"  {client}: {artifact['path']} (sha256 {artifact['sha256'][:16]}...)")
        print(f"Manifest written: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
