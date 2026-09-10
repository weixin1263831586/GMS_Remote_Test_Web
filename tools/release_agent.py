#!/usr/bin/env python3
"""One-version release bump for the GMS agent package (11.txt).

The release version is declared exactly once, in
``agent/gms-remote-test/package.yaml``. This script propagates it to every
generated declaration:

  * agent/gms-remote-test/runtime/gms-remote-test.sh  (GMS_RT_VERSION)
  * agent/gms-remote-test/runtime/mcp_server.py       (SERVER_VERSION)
  * agent/gms-remote-test/manifests/kk.plugin.json    ("version")
  * agent/gms-remote-test/manifests/kimi.plugin.json  ("version")
  * agent/gms-remote-test/manifests/codex.plugin.json ("version")

After bumping it regenerates the plugin payload via
tools/sync_agent_package.py (plugins/gms-remote-test/ is generated — never
edited directly).

Usage:
    python tools/release_agent.py --version 0.14.0
    python tools/release_agent.py --check      # verify all six agree
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent" / "gms-remote-test"
PACKAGE_YAML = AGENT_DIR / "package.yaml"
CLI_SCRIPT = AGENT_DIR / "runtime" / "gms-remote-test.sh"
MCP_SERVER = AGENT_DIR / "runtime" / "mcp_server.py"
MANIFESTS = [
    AGENT_DIR / "manifests" / "kk.plugin.json",
    AGENT_DIR / "manifests" / "kimi.plugin.json",
    AGENT_DIR / "manifests" / "codex.plugin.json",
]
SYNC_SCRIPT = REPO_ROOT / "tools" / "sync_agent_package.py"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_package_version() -> str:
    for line in PACKAGE_YAML.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^version: (\S+)$", line)
        if match:
            return match.group(1)
    raise SystemExit(f"Error: no 'version:' declaration in {PACKAGE_YAML}")


def set_package_version(version: str) -> None:
    text = PACKAGE_YAML.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"^version: \S+$", f"version: {version}", text, count=1, flags=re.M
    )
    if count != 1:
        raise SystemExit(f"Error: could not rewrite version in {PACKAGE_YAML}")
    PACKAGE_YAML.write_text(new_text, encoding="utf-8")


def replace_once(path: Path, pattern: str, replacement: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"Error: expected exactly one {label} in {path}, found {count}")
    path.write_text(new_text, encoding="utf-8")


def bump_manifest(path: Path, version: str) -> None:
    # Only the top-level "version" field (two-space indent), never nested ones.
    replace_once(path, r'^  "version": "[^"]*",$', f'  "version": "{version}",', "top-level version")


def collect_versions() -> dict[str, str]:
    def manifest_version(path: Path) -> str:
        match = re.search(r'^  "version": "([^"]*)",$', path.read_text(encoding="utf-8"), re.M)
        return match.group(1) if match else ""

    cli_match = re.search(r'^GMS_RT_VERSION="([^"]*)"$', CLI_SCRIPT.read_text(encoding="utf-8"), re.M)
    mcp_match = re.search(r'^SERVER_VERSION = "([^"]*)"$', MCP_SERVER.read_text(encoding="utf-8"), re.M)
    versions = {"package.yaml": read_package_version()}
    for path in MANIFESTS:
        versions[str(path.relative_to(REPO_ROOT))] = manifest_version(path)
    versions[str(CLI_SCRIPT.relative_to(REPO_ROOT))] = cli_match.group(1) if cli_match else ""
    versions[str(MCP_SERVER.relative_to(REPO_ROOT))] = mcp_match.group(1) if mcp_match else ""
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", help="new release version (X.Y.Z)")
    group.add_argument("--check", action="store_true", help="verify all declarations agree")
    args = parser.parse_args()

    if args.check:
        versions = collect_versions()
        expected = versions["package.yaml"]
        drifted = {k: v for k, v in versions.items() if v != expected}
        for key, value in versions.items():
            marker = "" if value == expected else "  != package.yaml"
            print(f"  {key}: {value}{marker}")
        if drifted:
            print(f"Error: version drift against package.yaml {expected}", file=sys.stderr)
            return 1
        print(f"Version contract OK: {expected}")
        return 0

    if not _VERSION_RE.match(args.version or ""):
        print(f"Error: --version must be X.Y.Z, got {args.version!r}", file=sys.stderr)
        return 2

    set_package_version(args.version)
    replace_once(
        CLI_SCRIPT, r'^GMS_RT_VERSION="[^"]*"$', f'GMS_RT_VERSION="{args.version}"', "GMS_RT_VERSION"
    )
    replace_once(
        MCP_SERVER, r'^SERVER_VERSION = "[^"]*"$', f'SERVER_VERSION = "{args.version}"', "SERVER_VERSION"
    )
    for manifest in MANIFESTS:
        bump_manifest(manifest, args.version)

    print(f"Bumped agent package to {args.version}; regenerating plugin payload ...")
    result = subprocess.run([sys.executable, str(SYNC_SCRIPT), str(REPO_ROOT)])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
