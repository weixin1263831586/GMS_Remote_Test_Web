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
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
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


@contextmanager
def _build_lock(out_base: Path):
    out_base.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(out_base / ".build.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _completed_version(directory: Path) -> bool:
    """A completed manifest must match every published artifact."""
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
        clients = manifest["clients"]
        if (manifest["name"] != "gms-remote-test" or manifest["version"] != directory.name
                or not isinstance(artifacts, dict) or not artifacts
                or set(clients) != set(artifacts) or not set(clients).issubset(CLIENT_MANIFESTS)):
            return False
        for client, artifact in artifacts.items():
            archive = directory / client / f"gms-remote-test-{directory.name}-{client}.zip"
            if archive.is_symlink() or archive.stat().st_size != artifact["size"]:
                return False
            digest = hashlib.sha256()
            with archive.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != artifact["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _prune_old_versions_locked(out_base: Path, current: str, keep: int) -> list[str]:
    """Delete stale version dirs, keeping the ``keep`` newest ones.

    The just-built version is always kept, even if fewer than ``keep``
    directories exist. Returns the pruned directory names.
    """
    if keep <= 0 or not out_base.is_dir():
        return []
    versions = [
        entry.name for entry in out_base.iterdir()
        if entry.is_dir() and not entry.is_symlink() and VERSION_DIR_RE.fullmatch(entry.name)
        and _completed_version(entry)
    ]
    keep_versions = {current, *sorted(versions, key=_version_sort_key, reverse=True)[:keep]}
    pruned = []
    for name in sorted(versions, key=_version_sort_key):
        if name in keep_versions:
            continue
        shutil.rmtree(out_base / name)
        pruned.append(name)
    return pruned


def prune_old_versions(out_base: Path, current: str, keep: int) -> list[str]:
    if keep <= 0 or not out_base.is_dir():
        return []
    with _build_lock(out_base):
        return _prune_old_versions_locked(out_base, current, keep)


def build_packages(out_base: Path, version: str, keep: int) -> dict[str, object]:
    if not VERSION_DIR_RE.fullmatch(version):
        raise ValueError("Invalid package version")
    out_base = out_base.resolve()
    out_root = out_base / version
    manifest: dict[str, object] = {
        "name": "gms-remote-test", "version": version, "clients": sorted(CLIENT_MANIFESTS),
    }
    # All writers and pruning share one process-safe lock, including builds
    # of an older version. Stage all archives before publishing any of them.
    with _build_lock(out_base), tempfile.TemporaryDirectory(prefix=".building-", dir=out_base) as temporary:
        stage = Path(temporary)
        artifacts = {}
        for client in sorted(CLIENT_MANIFESTS):
            data = build_package_bytes(PLUGIN_DIR, version, client=client)
            filename = f"gms-remote-test-{version}-{client}.zip"
            (stage / client).mkdir()
            (stage / client / filename).write_bytes(data)
            artifacts[client] = {
                "path": str(out_root / client / filename),
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            }
        manifest["artifacts"] = artifacts
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        for client in artifacts:
            (out_root / client).mkdir(parents=True, exist_ok=True)
            filename = f"gms-remote-test-{version}-{client}.zip"
            os.replace(stage / client / filename, out_root / client / filename)
        # The completion marker is published last. An interrupted publication
        # has mismatched hashes and cannot be treated as a completed version.
        os.replace(stage / "manifest.json", out_root / "manifest.json")
        for name in _prune_old_versions_locked(out_base, version, keep):
            print(f"Pruned old version dir: {out_base / name}")
    return manifest


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
    out_base = (REPO_ROOT / args.out / "gms-remote-test").resolve()
    try:
        manifest = build_packages(out_base, version, args.keep)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if args.print_manifest:
        print(json.dumps(manifest, indent=2))
    else:
        for client, artifact in manifest["artifacts"].items():
            print(f"  {client}: {artifact['path']} (sha256 {artifact['sha256'][:16]}...)")
        print(f"Manifest written: {out_base / version / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
