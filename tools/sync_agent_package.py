#!/usr/bin/env python3
"""sync_agent_package.py — generate plugins/gms-remote-test from agent/ (11.txt §二).

The single generation rule of the agent package system (11.txt):

    agent/gms-remote-test/   Source      — the ONLY hand-edited tree
    plugins/gms-remote-test/ Generated   — produced by this tool, never edited
    dist/gms-remote-test/    Build output — produced by build_agent_package.py

Mapping (source → generated):

    runtime/*                → plugins/gms-remote-test/scripts/*
    skill/*                  → plugins/gms-remote-test/skills/gms-remote-test/*
    manifests/kk.plugin.json   → plugins/gms-remote-test/kk.plugin.json
    manifests/kimi.plugin.json → plugins/gms-remote-test/kimi.plugin.json
    manifests/codex.plugin.json→ plugins/gms-remote-test/.codex-plugin/plugin.json
    tests/*                  → plugins/gms-remote-test/tests/*
    docs/README.md, docs/AGENTS.md → plugins/gms-remote-test/{README.md,AGENTS.md}
    (writes)                 → plugins/gms-remote-test/GENERATED.md

`scripts/install_local.sh` is a dev utility operating ON the generated tree
(it registers the plugin into kkagent); it is hand-maintained in place and
preserved by the prune step.

The tool also enforces:
  * six-way version contract (package.yaml / CLI / MCP / three manifests)
  * R16 same-version content guard (a version never silently changes content)

Usage:
    python tools/sync_agent_package.py [repo_root]
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path


PLUGIN_ID = "gms-remote-test"
PRESERVED_IN_PLUGIN = {"scripts/install_local.sh", "__pycache__"}


def repo_root_arg() -> Path:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    return root


class SyncError(SystemExit):
    pass


def fail(message: str) -> None:
    raise SyncError(f"Error: {message}")


def read_version(path: Path, pattern: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.M)
    return match.group(1) if match else ""


def sync_one(source: Path, target: Path) -> bool:
    """Copy when changed; returns True when a copy happened."""
    if not source.is_file():
        fail(f"source missing: {source}")
    if target.is_file() and target.read_bytes() == source.read_bytes():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    if source.suffix == ".sh" or not source.suffix:
        target.chmod(0o755)
    print(f"Synced {target.name}")
    return True


def sync_tree(source_dir: Path, target_dir: Path) -> None:
    if not source_dir.is_dir():
        fail(f"source directory missing: {source_dir}")
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        rel = source.relative_to(source_dir)
        sync_one(source, target_dir / rel)


def prune_stale(plugin_dir: Path, expected: set[str]) -> list[str]:
    """Remove generated files that are no longer produced (keep PRESERVED)."""
    removed = []
    for path in sorted(plugin_dir.rglob("*"), reverse=True):
        rel = path.relative_to(plugin_dir).as_posix()
        if any(rel == keep or rel.startswith(keep + "/") for keep in PRESERVED_IN_PLUGIN):
            continue
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
            continue
        if path.is_file() and rel not in expected:
            path.unlink()
            removed.append(rel)
    return removed


def r16_guard(root: Path, source: Path, version: str, pattern: str, label: str) -> None:
    """A version must never silently change content (10.txt §二十一)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{source.relative_to(root).as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return  # new path — nothing to compare against
    previous = result.stdout
    match = re.search(pattern, previous, re.M)
    if not match or match.group(1) != version:
        return  # HEAD carried a different version — content change is expected
    if hashlib.sha256(previous.encode()).hexdigest() != hashlib.sha256(
        source.read_bytes()
    ).hexdigest():
        fail(
            f"{label} content changed at the same version ({version}). "
            "Bump the version with tools/release_agent.py, then re-run sync."
        )


GENERATED_MD = """# GENERATED — DO NOT EDIT THIS DIRECTORY DIRECTLY

Everything under `plugins/gms-remote-test/` is a generated release payload.

- Generated from: `agent/gms-remote-test/`
- Regenerate with: `python tools/sync_agent_package.py`
- Release flow: `python tools/release_agent.py --version X.Y.Z`

The only hand-maintained exception is `scripts/install_local.sh`, a dev
utility that registers THIS generated tree into a local kkagent.
"""


def main() -> int:
    root = repo_root_arg()
    agent_dir = root / "agent" / PLUGIN_ID
    plugin_dir = root / "plugins" / PLUGIN_ID

    runtime = agent_dir / "runtime"
    skill = agent_dir / "skill"
    manifests = agent_dir / "manifests"
    tests = agent_dir / "tests"
    docs = agent_dir / "docs"

    for required in (runtime, skill, manifests, tests, docs):
        if not required.is_dir():
            fail(f"agent package source missing: {required}")

    # --- version contract (six declarations) ----------------------------
    package_yaml = agent_dir / "package.yaml"
    package_version = read_version(package_yaml, r"^version: (\S+)$")
    cli_version = read_version(runtime / "gms-remote-test.sh", r'^GMS_RT_VERSION="([^"]+)"$')
    mcp_version = read_version(runtime / "mcp_server.py", r'^SERVER_VERSION = "([^"]+)"$')
    manifest_files = {
        "kk": manifests / "kk.plugin.json",
        "kimi": manifests / "kimi.plugin.json",
        "codex": manifests / "codex.plugin.json",
    }
    manifest_versions = {
        key: read_version(path, r'^  "version": "([^"]+)",$')
        for key, path in manifest_files.items()
    }
    declarations = {
        "package.yaml": package_version,
        "gms-remote-test.sh": cli_version,
        "mcp_server.py": mcp_version,
        **{f"manifests/{k}.plugin.json": v for k, v in manifest_versions.items()},
    }
    missing = [name for name, value in declarations.items() if not value]
    if missing:
        fail(f"unable to read version declarations: {missing}")
    drifted = {name: value for name, value in declarations.items() if value != package_version}
    if drifted:
        for name, value in declarations.items():
            marker = "" if value == package_version else "  != package.yaml"
            print(f"  {name}: {value}{marker}", file=sys.stderr)
        fail(f"version drift against package.yaml {package_version}; fix with tools/release_agent.py")

    # --- R16 same-version content guard ---------------------------------
    in_git = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    ).returncode == 0
    if in_git:
        r16_guard(root, runtime / "gms-remote-test.sh", cli_version,
                  r'^GMS_RT_VERSION="([^"]+)"$', "gms-remote-test.sh")
        r16_guard(root, runtime / "mcp_server.py", mcp_version,
                  r'^SERVER_VERSION = "([^"]+)"$', "mcp_server.py")

    # --- generate --------------------------------------------------------
    expected: set[str] = set()

    def rel_plugin(*parts: str) -> str:
        name = "/".join(parts)
        expected.add(name)
        return name

    # runtime/* → scripts/*
    for source in sorted(runtime.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        sync_one(source, plugin_dir / rel_plugin("scripts", source.relative_to(runtime).as_posix()))
    # skill/* → skills/gms-remote-test/*
    for source in sorted(skill.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        sync_one(source, plugin_dir / rel_plugin("skills", PLUGIN_ID, source.relative_to(skill).as_posix()))
    # manifests → plugin root layout
    sync_one(manifest_files["kk"], plugin_dir / rel_plugin("kk.plugin.json"))
    sync_one(manifest_files["kimi"], plugin_dir / rel_plugin("kimi.plugin.json"))
    sync_one(manifest_files["codex"], plugin_dir / rel_plugin(".codex-plugin", "plugin.json"))
    # tests/*
    for source in sorted(tests.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        sync_one(source, plugin_dir / rel_plugin("tests", source.relative_to(tests).as_posix()))
    # docs → plugin root
    sync_one(docs / "README.md", plugin_dir / rel_plugin("README.md"))
    sync_one(docs / "AGENTS.md", plugin_dir / rel_plugin("AGENTS.md"))
    # GENERATED.md
    generated_md = plugin_dir / "GENERATED.md"
    if not generated_md.is_file() or generated_md.read_text(encoding="utf-8") != GENERATED_MD:
        generated_md.write_text(GENERATED_MD, encoding="utf-8")
        print("Synced GENERATED.md")
    expected.add("GENERATED.md")

    removed = prune_stale(plugin_dir, expected)
    for rel in removed:
        print(f"Pruned stale: {rel}")

    print(f"Version contract OK: {package_version} (6 declarations, plugin payload synced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
