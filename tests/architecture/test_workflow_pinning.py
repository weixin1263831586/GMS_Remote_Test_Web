"""GitHub Actions 引用完整性门禁（workflow pinning gate）。

供应链加固约束：.github/workflows/ 下所有 `uses:` 引用必须 pin 到完整
commit SHA（40 位十六进制），并以 `# vX.Y.Z` 注释保留版本可读性。浮动
tag（`actions/checkout@v4`）可被上游 repo 的写权限静默改写，是已知的
GitHub Actions 供应链攻击面（tj-actions/changed-events 事件即属此类）。

规则：

- 每条 `uses: <owner>/<repo>@<ref>` 的 `<ref>` 必须是 40 位小写十六进制
  SHA-1 commit id；
- 本地 `./path` 或 `docker://` 引用不受此约束（无上游改写面）；
- SHA 后必须带 `# vX.Y.Z`（或语义化等价物）注释，保证升级评审时可读。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)\s*(.*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_COMMENT_RE = re.compile(r"#\s*v\d+(?:\.\d+)*\S*$")
# 本地路径 action 与 docker 引用没有上游 tag 改写面，放行。
NON_REMOTE_PREFIXES = ("./", "docker://")


class WorkflowPinningTests(unittest.TestCase):
    def _workflow_uses(self) -> list[tuple[Path, int, str, str]]:
        """收集 (文件, 行号, 引用, 行尾注释)。"""

        entries: list[tuple[Path, int, str, str]] = []
        workflow_paths = sorted(
            path
            for pattern in ("*.yml", "*.yaml")
            for path in WORKFLOWS_DIR.glob(pattern)
        )
        for path in workflow_paths:
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = USES_RE.match(line)
                if match:
                    entries.append(
                        (path, lineno, match.group(1), match.group(2))
                    )
        return entries

    def test_all_remote_actions_are_pinned_to_full_sha(self):
        offenders: list[str] = []
        found_any = False
        for path, lineno, ref, _tail in self._workflow_uses():
            if ref.startswith(NON_REMOTE_PREFIXES):
                continue
            found_any = True
            if "@" not in ref:
                offenders.append(f"{path.name}:{lineno}: 缺少 @ref: {ref}")
                continue
            _owner_repo, _, sha = ref.rpartition("@")
            if not SHA_RE.match(sha):
                offenders.append(f"{path.name}:{lineno}: 未 pin 到完整 SHA: {ref}")
        self.assertTrue(found_any, "workflows 目录应至少存在一条 uses: 引用")
        self.assertEqual(offenders, [])

    def test_pinned_sha_keeps_version_comment(self):
        offenders: list[str] = []
        for path, lineno, ref, tail in self._workflow_uses():
            if ref.startswith(NON_REMOTE_PREFIXES):
                continue
            _owner_repo, _, sha = ref.rpartition("@")
            if SHA_RE.match(sha) and not VERSION_COMMENT_RE.search(tail):
                offenders.append(
                    f"{path.name}:{lineno}: SHA pin 缺少 '# vX.Y.Z' 版本注释: {ref}"
                )
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
