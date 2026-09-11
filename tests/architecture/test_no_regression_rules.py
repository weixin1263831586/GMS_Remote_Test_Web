"""Architecture regression guards (12.txt §二/§十/§十一/§十七).

These tests encode the "no architecture regression" rules called out by the
2026-09-10 source audit.  They are intentionally static-text based (cheap,
dependency-free) so every regression fails CI at the exact line:

1. ``ssh.exec_command(`` — business modules must execute SSH commands via
   ``foundation.ssh_executor``.  Direct calls reintroduce the
   stdout/stderr channel-window deadlock the executor was written to fix.
2. Personal-environment hardcodes — ``C:\\Users\\<name>`` fallbacks and
   ``172.16.*`` internal addresses must never return (fail closed instead).
3. README must not resurrect removed firmware backends
   (``transport-probe-force`` / ``burn_mode`` fastboot/partition).
4. The generated plugin tree must not resurrect the retired legacy
   launcher (``mcp_launcher.sh``); ``mcp_launcher.py`` is canonical.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

SCAN_DIRS = ("features", "foundation", "worker_agent", "workflows", "bootstrap")

# Business modules may never call exec_command directly.
EXEC_COMMAND_ALLOWED_FILES = {
    # The canonical implementation itself.
    "foundation/ssh_executor.py",
    # Registered connection-health primitive: needs raw channel timeout
    # semantics (recv_exit_status has no timeout) — see module docstring.
    "features/system/ssh.py",
}

PERSONAL_PATTERNS = {
    r"C:\\Users\\[A-Za-z0-9_]+\\": "hardcoded personal Windows profile path",
    r"172\.16\.\d+\.\d+": "internal network address (use RFC 5737 TEST-NET)",
    r"\bhcq\b": "personal username",
}

README_FORBIDDEN = {
    r"transport-probe-force\s+在目标|`transport-probe-force`\s*走": (
        "removed transport-probe-force firmware backend"
    ),
    r"burn_mode.*`fastboot`|`fastboot`.*两阶段\s*Fastboot\s*完整烧写": (
        "removed fastboot burn_mode backend"
    ),
    r"`partition`\s*走\s*\*\*同会话\s*DI": (
        "removed partition burn_mode backend"
    ),
}

AGENT_RUNTIME_DIR = ROOT / "agent" / "gms-remote-test" / "runtime"
PLUGIN_DIR = ROOT / "plugins" / "gms-remote-test"


class SshExecutionBoundaryTests(unittest.TestCase):
    def test_business_modules_use_ssh_executor(self):
        violations: list[str] = []
        for scan_dir in SCAN_DIRS:
            base = ROOT / scan_dir
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if "__pycache__" in path.parts or "/tests/" in path.as_posix():
                    continue
                relative = path.relative_to(ROOT).as_posix()
                if relative in EXEC_COMMAND_ALLOWED_FILES:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for lineno, line in enumerate(
                    text.splitlines(), start=1
                ):
                    if ".exec_command(" in line:
                        violations.append(f"{relative}:{lineno}: {line.strip()[:100]}")
        self.assertEqual(
            violations,
            [],
            "direct ssh.exec_command() outside the unified executor:\n"
            + "\n".join(violations)
            + "\n\nUse foundation.ssh_executor (SSHExecutor / "
            "SSHManager.execute_command); add to EXEC_COMMAND_ALLOWED_FILES "
            "only for audited channel-level primitives.",
        )


class PersonalEnvironmentHardcodeTests(unittest.TestCase):
    def test_no_personal_hosts_usernames_or_addresses(self):
        violations: list[str] = []
        for scan_dir in SCAN_DIRS:
            base = ROOT / scan_dir
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                # Unit tests may use fixture usernames; production code may not.
                if "/tests/" in path.as_posix() or path.name.startswith("test_"):
                    continue
                relative = path.relative_to(ROOT).as_posix()
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    for pattern, reason in PERSONAL_PATTERNS.items():
                        if re.search(pattern, line):
                            violations.append(
                                f"{relative}:{lineno}: {reason}: {line.strip()[:90]}"
                            )
        self.assertEqual(
            violations,
            [],
            "personal environment hardcodes found:\n" + "\n".join(violations),
        )


class ReadmeCurrentArchitectureTests(unittest.TestCase):
    def test_readme_does_not_resurrect_removed_backends(self):
        readme = ROOT / "README.md"
        text = readme.read_text(encoding="utf-8")
        violations = []
        for pattern, reason in README_FORBIDDEN.items():
            if re.search(pattern, text):
                violations.append(reason)
        self.assertEqual(
            violations,
            [],
            "README describes removed firmware backends: " + "; ".join(violations),
        )


class AgentLauncherSingleSourceTests(unittest.TestCase):
    def test_legacy_launcher_is_gone_everywhere(self):
        missing = []
        for base in (AGENT_RUNTIME_DIR, PLUGIN_DIR / "scripts"):
            if base.is_dir() and (base / "mcp_launcher.sh").exists():
                missing.append(str((base / "mcp_launcher.sh").relative_to(ROOT)))
        self.assertEqual(
            missing,
            [],
            "legacy mcp_launcher.sh must stay deleted (python launcher is "
            "canonical, see 12.txt P1 profile migration): " + ", ".join(missing),
        )

    def test_package_yaml_points_python_launcher(self):
        package_yaml = ROOT / "agent" / "gms-remote-test" / "package.yaml"
        text = package_yaml.read_text(encoding="utf-8")
        self.assertNotIn("mcp_launcher.sh", text)
        self.assertIn("launcher: runtime/mcp_launcher.py", text)


if __name__ == "__main__":
    unittest.main()
