#!/usr/bin/env python3
"""sync_agent_package.py — generate plugins/gms-remote-test from agent/.

The single generation rule of the agent package system:

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
    templates/README.md → plugins/gms-remote-test/README.md
    (the payload README is a distribution template, never an AGENTS.md under
    the canonical docs/ tree)
    templates/PLUGIN_AGENTS.md → plugins/gms-remote-test/AGENTS.md
    (never a docs/AGENTS.md: an AGENTS.md under the canonical tree would
    override root instructions for Codex/Kimi agents working there)
    (writes)                 → plugins/gms-remote-test/GENERATED.md

`scripts/install_local.sh` is a dev utility operating ON the generated tree
(it registers the plugin into kkagent); it is hand-maintained in place and
preserved by the prune step.

The tool also enforces:
  * six-way version contract (package.yaml / CLI / MCP / three manifests)
  * same-version content guard (a version never silently changes content);
    See docs/architecture/adr/0003-agent-profile-store.md.

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
    """Copy when changed; returns True when a copy happened.

    Historically, an early return on identical content used to leave a
    WRONG executable bit unfixed forever (e.g. the manifests exec
    ./scripts/mcp_launcher.py directly, but a chmod had been lost on the
    generated side). Even when content matches, mirror the executable bit
    the source demands.
    """
    if not source.is_file():
        fail(f"source missing: {source}")
    if target.is_file() and target.read_bytes() == source.read_bytes():
        want_mode = 0o755 if _is_executable_source(source) else 0o644
        if target.stat().st_mode & 0o777 != want_mode:
            target.chmod(want_mode)
            print(f"Fixed file mode: {target.name}")
            return True
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    target.chmod(0o755 if _is_executable_source(source) else 0o644)
    print(f"Synced {target.name}")
    return True


def _is_executable_source(source: Path) -> bool:
    """The manifests exec ./scripts/mcp_launcher.py
    directly, so shebang-carrying .py launchers must keep the executable
    bit in the generated tree (not just .sh / extension-less files)."""
    if source.suffix == ".sh" or not source.suffix:
        return True
    try:
        with source.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


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
    """A version must never silently change content."""
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


def _head_package_version(root: Path, package_yaml: Path) -> str:
    """HEAD 提交里的 canonical package 版本（读不到返回空）。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show",
             f"HEAD:{package_yaml.relative_to(root).as_posix()}"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return ""
    match = re.search(r"^version: (\S+)$", result.stdout, re.M)
    return match.group(1) if match else ""


def r16_tree_guard(
    root: Path, source_dir: Path, version: str, label: str, package_yaml: Path
) -> None:
    """Whole-tree same-version content guard.

    ``r16_guard`` above only compares two anchor files; a same-version
    change to ANY other payload file (package_manager.py, mcp_launcher.py,
    SKILL.md, manifests, docs …) slipped through, so two machines could run
    different content while both reported the same version and installed
    hosts skipped re-download ("Already up to date"). This walks the whole
    payload tree, hashes every file plus its archive-relative path, and
    compares against HEAD. Only text files enter the anchor comparison, so
    we hash bytes here (rb) — binary-identical to the release builder's
    view of the tree.

    升版本放行以 HEAD 的 canonical package version（package.yaml）为准：
    正常发布必然 bump 版本并同步修改 manifests/SKILL 等 payload 文件，
    此时树内容相对 HEAD 必然漂移，属于预期；只有「HEAD 版本 == 当前
    版本」时的内容变化才禁止（回归：旧的 no-anchor 分支只比较树哈希，
    未核对 HEAD 版本，导致任何升版本后的首次 sync 都被误拦）。
    """
    digests: list[str] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(source_dir).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digests.append(f"{rel}:{digest}")
    tree_hash = hashlib.sha256("\n".join(digests).encode()).hexdigest()

    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-r", "--name-only",
             f"HEAD:{source_dir.relative_to(root).as_posix()}"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return  # new tree — nothing to compare against
    previous_digests: list[str] = []
    for rel in sorted(listing.stdout.split()):
        if "__pycache__" in rel:
            continue
        show = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{source_dir.relative_to(root).as_posix()}/{rel}"],
            capture_output=True, check=True,
        )
        previous_digests.append(f"{rel}:{hashlib.sha256(show.stdout).hexdigest()}")
    previous_hash = hashlib.sha256("\n".join(previous_digests).encode()).hexdigest()
    if tree_hash == previous_hash:
        return

    # HEAD 是旧版本 → 内容漂移是本次发布的预期结果。
    if _head_package_version(root, package_yaml) != version:
        return
    fail(
        f"{label} tree content changed at the same version ({version}). "
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

    # --- same-version content guard --------------------------------------
    # A version must never silently change payload content.
    # See docs/architecture/adr/0003-agent-profile-store.md.
    in_git = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    ).returncode == 0
    if in_git:
        r16_guard(root, runtime / "gms-remote-test.sh", cli_version,
                  r'^GMS_RT_VERSION="([^"]+)"$', "gms-remote-test.sh")
        r16_guard(root, runtime / "mcp_server.py", mcp_version,
                  r'^SERVER_VERSION = "([^"]+)"$', "mcp_server.py")
        # The anchor checks above only cover two files; the tree guards
        # below close the same-version drift hole for the WHOLE
        # distributable payload (审核意见：release identity 不能只覆盖
        # runtime/ — SKILL/manifests/templates 同版本变更同样会造成
        # "version 未变但内容变了" 的安装漂移)。runtime、skill、
        # manifests 与 templates（README/PLUGIN_AGENTS，随包发布）都在
        # 守护范围内；docs/tests 不参与发布身份，仅 runtime 侧已覆盖。
        # 升版本放行统一按 HEAD 的 package.yaml canonical 版本判定。
        r16_tree_guard(root, runtime, cli_version, "runtime/", package_yaml)
        r16_tree_guard(root, skill, cli_version, "skill/", package_yaml)
        r16_tree_guard(root, manifests, cli_version, "manifests/", package_yaml)
        r16_tree_guard(root, agent_dir / "templates", cli_version, "templates/", package_yaml)
        # AGENT_PLAYBOOK.md 是发布载荷（同步进 plugin docs/），同版本内容
        # 漂移同样造成 "version 未变但内容变了" 的安装漂移（审核意见）。
        playbook = docs / "AGENT_PLAYBOOK.md"
        if in_git and playbook.is_file():
            r16_tree_guard(root, docs, cli_version, "docs/", package_yaml)

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
    # docs → plugin root. Both payload docs are sourced from templates/ so
    # the canonical tree never carries files that look like generated-plugin
    # instructions (they would override the root AGENTS.md for Codex/Kimi
    # sessions working under agent/).
    sync_one(
        agent_dir / "templates" / "README.md",
        plugin_dir / rel_plugin("README.md"),
    )
    sync_one(
        agent_dir / "templates" / "PLUGIN_AGENTS.md",
        plugin_dir / rel_plugin("AGENTS.md"),
    )
    # The agent playbook is part of the published payload —
    # without this line the playbook only ever exists in the source tree
    # and agents downloading from the registry never see it.
    sync_one(docs / "AGENT_PLAYBOOK.md", plugin_dir / rel_plugin("docs", "AGENT_PLAYBOOK.md"))
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
