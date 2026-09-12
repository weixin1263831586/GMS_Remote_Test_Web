"""评审标记残留门禁（review-marker gate）。

docs/development.md「文档政策」的工程化约束：源码注释不得引用仓库中
不存在的评审文档编号。历史上多轮评审（audit/plan/工单编号、`2.txt`、
`Redmine.txt` 等）在源码里留下大量 `2026-09-08 audit §五`、`P1-7` 一类
标记——文档早已删除，注释却仍在，误导后来者与 Agent。

规则（对生产源码树扫描）：

- `\\d+\\.txt` / `Redmine\\.txt`  —— 引用仓库外的评审/需求文本；
- `§`                             —— 审计/计划分节引用；
- `P\\d+-\\d+` / `R\\d+-\\d+`     —— 评审轮次编号；
- `<日期> audit` / `audit follow-up|fix|round` —— 带日期的审计引用
  （不扫描裸 `audit` 一词：安全审计日志功能 security_audit 本身是合法
  领域词汇）；
- `review round` / `评审意见` / `评审第` 之类评审轮次措辞。

已清洗为真实出处的引用不受影响：架构决策引用 `ADR NNNN`（见
docs/architecture/adr/），历史行为说明直接描述行为本身。

白名单：EXCEPTIONS 中的相对路径允许出现上述模式（必须写明理由与到期
清理条件）。

注意：刻意不做裸 `R\\d+` 扫描——GTS/VTS 套件名（如 ``android-gts-14-R2``）
等合法标识符会大量误报。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# 生产源码树：features/foundation/bootstrap/worker_agent/workflows 的全部
# .py（含各自 tests/），agent 源树，顶层 .py 与 scripts/tools 的 .py，
# 以及 docs/ 的文档——文档同样不得引用不存在的评审轮次。
SCAN_ROOTS = [
    ROOT / "features",
    ROOT / "foundation",
    ROOT / "bootstrap",
    ROOT / "worker_agent",
    ROOT / "workflows",
    ROOT / "agent" / "gms-remote-test",
    ROOT / "scripts",
    ROOT / "tools",
    ROOT / "docs",
]
TOP_LEVEL_PY = sorted((ROOT / name).name for name in
                      ("app.py", "conftest.py") if (ROOT / name).is_file())

# 除 .py 外同样受门禁约束的文件：agent 源树与 scripts 的 shell、前端
# JS/HTML、以及全部扫描根内的 Markdown 文档。生成树 plugins/ 不在此列
# （由 drift gate 保证与源树一致）。
SCAN_SUFFIXES = {".py", ".sh", ".js", ".html", ".md"}


# 允许出现评审标记的文件（相对 ROOT）。必须写明理由，且随清理移除。
EXCEPTIONS: set[str] = {
    # scripts/verify_release_tree.py 的 DENIED_NAMES 刻意罗列 scratch 文件名
    # （1.txt/2.txt/3.txt）作为发布树拒绝项——这是发布门禁的数据，不是
    # 评审引用。
    "scripts/verify_release_tree.py",
}

FORBIDDEN_PATTERNS = [
    (re.compile(r"\b\d+\.txt\b"), "scratch/review .txt reference"),
    (re.compile(r"\bRedmine\.txt\b"), "external Redmine.txt reference"),
    (re.compile(r"§"), "audit/plan section sign"),
    (re.compile(r"\bP\d+-\d+\b"), "review-round ID"),
    (re.compile(r"\bR\d+-\d+\b"), "review-round ID"),
    # 带日期的审计引用（"2026-09-08 audit"）。裸 "audit" 不扫描：
    # security_audit 审计日志是合法领域词汇。
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\s+audit\b", re.IGNORECASE), "dated audit citation"),
    (re.compile(r"\baudit\s+(?:follow-up|fix|round)\b", re.IGNORECASE), "audit citation"),
    (re.compile(r"review round", re.IGNORECASE), "review-round wording"),
    (re.compile(r"评审意见|评审第|工单\s*P"), "review-round wording"),
]

_EXCLUDED_DIR_NAMES = {"__pycache__", ".venv", "node_modules", "dist"}


def _candidate_files() -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for base in SCAN_ROOTS:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() not in SCAN_SUFFIXES or not path.is_file():
                continue
            if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
                continue
            if path in seen:
                continue
            seen.add(path)
            files.append(path)
    for name in TOP_LEVEL_PY:
        path = ROOT / name
        if path.is_file() and path not in seen:
            seen.add(path)
            files.append(path)
    return sorted(files)


class ReviewMarkerTests(unittest.TestCase):
    def test_production_source_has_no_review_markers(self):
        offenders: list[str] = []
        for path in _candidate_files():
            relative = str(path.relative_to(ROOT)).replace("\\", "/")
            if relative in EXCEPTIONS:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for pattern, why in FORBIDDEN_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        offenders.append(
                            f"{relative}:{lineno}: {why} {match.group(0)!r}"
                        )
                        break  # one report per line is enough
        self.assertEqual(offenders, [])

    def test_exception_list_still_exists(self):
        """白名单里的每一项必须仍然存在，防止例外条目腐化。"""

        missing = [
            item for item in EXCEPTIONS if not (ROOT / item).is_file()
        ]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
