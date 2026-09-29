"""Architecture rule: no direct shell-string execution in business code.

``shell=True`` must stay confined to two audited boundaries:

- ``foundation/processes.py`` — the shared ``run_local_shell_command`` helper
  for genuinely shell-shaped work (pipelines/redirection); everything else
  should use the argv-based ``run_local_command``.
- ``features/build/executor.py`` — build templates rendered from whitelisted
  ``trusted_shell_fragment`` values (choices/fullmatch-validated).

Any new ``shell=True`` elsewhere (raw subprocess/os.system/os.popen) fails
this test so injection-prone call sites cannot creep back in.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ALLOWED_SHELL_TRUE_FILES = {
    "foundation/processes.py",
    "features/build/executor.py",
}
SCAN_DIRS = ("features", "foundation", "worker_agent", "workflows", "bootstrap")
# 受控逃生口：确因模板/生成物而无法用 ast 解析的源码文件才登记在此，
# 默认必须为空——否则损坏文件会静默绕过 shell 边界检查。
UNPARSEABLE_ALLOWLIST: set[str] = set()


class _ShellTrueVisitor(ast.NodeVisitor):
    def __init__(self, display_path: str) -> None:
        self.path = display_path
        self.violations: list[str] = []

    def _check_keyword(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                if keyword.value.value is True:
                    self.violations.append(
                        f"{self.path}:{node.lineno} shell=True in call to "
                        f"{ast.dump(node.func)[:60]}..."
                    )

    def visit_Call(self, node: ast.Call) -> None:
        self._check_keyword(node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # os.system / os.popen are shell-string APIs without a keyword flag.
        if isinstance(node.value, ast.Name) and node.value.id == "os":
            if node.attr in {"system", "popen"}:
                self.violations.append(
                    f"{self.path}:{node.lineno} os.{node.attr}() is a shell-string API; "
                    "use foundation.processes.run_local_command instead"
                )
        self.generic_visit(node)


class ShellExecutionBoundaryTests(unittest.TestCase):
    def test_shell_true_only_in_audited_boundaries(self):
        violations: list[str] = []
        parse_errors: list[str] = []
        for scan_dir in SCAN_DIRS:
            for path in sorted((ROOT / scan_dir).rglob("*.py")):
                if "tests" in path.relative_to(ROOT).parts:
                    continue
                relative = path.relative_to(ROOT).as_posix()
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (OSError, SyntaxError) as exc:
                    # 损坏/不可读文件不得静默绕过：未登记豁免即翻红。
                    if relative not in UNPARSEABLE_ALLOWLIST:
                        parse_errors.append(f"{relative}: {exc}")
                    continue
                if relative in ALLOWED_SHELL_TRUE_FILES:
                    continue
                visitor = _ShellTrueVisitor(relative)
                visitor.visit(tree)
                violations.extend(visitor.violations)
        self.assertEqual(
            parse_errors,
            [],
            "unparseable source files in shell-boundary scan dirs (would silently "
            "bypass the shell=True gate); register in UNPARSEABLE_ALLOWLIST only if "
            "genuinely a template/generated artifact:\n" + "\n".join(parse_errors),
        )
        self.assertEqual(
            violations,
            [],
            "shell=True / os.system found outside audited boundaries:\n"
            + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
