"""Agent package packaging tests (10.txt §二十一, §七/§八).

Gates the self-contained plugin payload: the plugin must carry the Skill
(10.txt §七), all three manifests must exist and agree on one version, the
six version declarations must match package.yaml, and no legacy auth
semantics may leak into the payload.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = REPO_ROOT / "agent" / "gms-remote-test"
PLUGIN_DIR = REPO_ROOT / "plugins" / "gms-remote-test"
SKILL_DIR = AGENT_DIR / "skill"
PACKAGE_YAML = AGENT_DIR / "package.yaml"
SYNC_SCRIPT = REPO_ROOT / "tools" / "sync_agent_package.py"


def package_version() -> str:
    for line in PACKAGE_YAML.read_text(encoding="utf-8").splitlines():
        if line.startswith("version: "):
            return line.split(":", 1)[1].strip()
    raise AssertionError("package.yaml missing version")


def test_plugin_is_self_contained_skill_plus_mcp():
    # 10.txt §七: installing the plugin must deliver MCP tools AND the Skill.
    assert (PLUGIN_DIR / "skills" / "gms-remote-test" / "SKILL.md").is_file()
    assert (PLUGIN_DIR / "scripts" / "mcp_server.py").is_file()
    assert (PLUGIN_DIR / "scripts" / "gms-remote-test.sh").is_file()


def test_kimi_manifest_declares_skills_and_launcher():
    manifest = json.loads((PLUGIN_DIR / "kimi.plugin.json").read_text(encoding="utf-8"))
    # 10.txt §八: Kimi native plugin must bundle the skill directory.
    assert manifest["skills"] == "./skills/"
    command = manifest["mcpServers"]["gms"]["command"]
    # 10.txt §十八: the launcher is the Python edition (reads TOML profiles).
    assert "mcp_launcher.py" in command, "MCP must launch through the runtime launcher"
    assert manifest["mcpServers"]["gms"]["env"]["GMS_AGENT_CLIENT"] == "kimi"


def test_codex_native_manifest_exists():
    # 10.txt §九: Codex native plugin packaging.
    manifest_path = PLUGIN_DIR / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "gms-remote-test"
    assert manifest["skills"] == "./skills/"


def test_six_way_version_contract():
    version = package_version()
    cli = (AGENT_DIR / "runtime" / "gms-remote-test.sh").read_text(
        encoding="utf-8"
    )
    mcp = (AGENT_DIR / "runtime" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    assert f'GMS_RT_VERSION="{version}"' in cli
    assert f'SERVER_VERSION = "{version}"' in mcp
    for name in ("kk.plugin.json", "kimi.plugin.json", ".codex-plugin/plugin.json"):
        manifest = json.loads((PLUGIN_DIR / name).read_text(encoding="utf-8"))
        assert manifest["version"] == version, name


def test_plugin_payload_matches_skill_source():
    # Every generated file must be byte-identical to its source (11.txt):
    # runtime/* → scripts/*, skill/* → skills/gms-remote-test/*.
    RUNTIME_DIR = REPO_ROOT / "agent" / "gms-remote-test" / "runtime"
    pairs = [
        ("scripts/gms-remote-test.sh", RUNTIME_DIR / "gms-remote-test.sh"),
        ("scripts/mcp_server.py", RUNTIME_DIR / "mcp_server.py"),
        ("scripts/mcp_launcher.py", RUNTIME_DIR / "mcp_launcher.py"),
        ("scripts/agent_mcp_config.py", RUNTIME_DIR / "agent_mcp_config.py"),
        ("scripts/gms-agent", RUNTIME_DIR / "gms-agent"),
        ("skills/gms-remote-test/SKILL.md", SKILL_DIR / "SKILL.md"),
    ]
    for plugin_rel, source_file in pairs:
        plugin_file = PLUGIN_DIR / plugin_rel
        assert plugin_file.read_bytes() == source_file.read_bytes(), plugin_rel


def test_sync_package_idempotent():
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), str(REPO_ROOT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Version contract OK" in result.stdout


def test_no_legacy_authorized_true_in_payload_docs():
    # 10.txt P0: agent-facing docs must never teach the retired authorized=true.
    for doc in PLUGIN_DIR.rglob("*.md"):
        text = doc.read_text(encoding="utf-8")
        # Fold line wraps first, then evaluate per sentence, so historical
        # "was never a security boundary" explanations don't false-positive.
        folded = " ".join(text.split())
        for sentence in re.split(r"[.!?]", folded):
            if "authorized=true" not in sentence:
                continue
            normalized = sentence.lower()
            assert (
                "never a security" in normalized
                or "no longer" in normalized
                or "no `authorized=true`" in normalized
            ), f"{doc}: {sentence.strip()}"


def test_build_agent_package_manifest_shape():
    result = subprocess.run(
        ["python3", str(REPO_ROOT / "tools" / "build_agent_package.py"), "--print-manifest"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["name"] == "gms-remote-test"
    assert manifest["version"] == package_version()
    artifact = manifest["artifacts"]["universal"]
    assert set(artifact) == {"path", "sha256", "size"}
    assert len(artifact["sha256"]) == 64
