#!/usr/bin/env python3
"""Check tracked source comments and docstrings for stale debugging narratives.

No files are modified. CI fails on high-confidence historical comments, but
only reports ambiguous references to previous runtime state for human review.
Business fixture dates and normal runtime strings are not treated as comments.
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common import find_repo_root

ROOT = find_repo_root()
CODE_SUFFIXES = {".py", ".js", ".css", ".html", ".sh", ".ts", ".tsx", ".rs"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "actions-runner", "dist", "target"}
MARKERS = (
    ("screenshot-timestamp", re.compile(r"20\d{6,12}.{0,30}(?:截图|复现|修复|排查|实测)|(?:截图|复现|修复|排查|实测).{0,30}20\d{6,12}")),
    ("version-changelog", re.compile(r"^\s*(?:v\d{1,3}|版本\d{1,3})(?:\s+|[:：])")),
    ("debugging-history", re.compile(r"(?:截图复现|夜批实测|首跑实测|某次截图|本次截图|修复前|修复过程|之前的实现|之前的代码|当时修复|遗留修复|临时排查|仅供临时|目测探针)")),
)
# These expressions identify review candidates, not unconditional deletions.
ADVISORY = re.compile(
    r"(?:截图.{0,12}(?:红框|绿框)|(?:红框|绿框).{0,12}截图|"
    r"^\s*v\d{1,3}\s*[:：]|历史修改|旧版逻辑|原先的实现|"
    r"作为临时修复|前一轮|上一轮|现场截图|曾经.*(?:bug|问题))",
    re.IGNORECASE,
)


def python_comments(path: Path, data: str):
    try:
        for token in tokenize.generate_tokens(io.StringIO(data).readline):
            if token.type == tokenize.COMMENT:
                yield token.start[0], token.string.lstrip("# ")
            elif token.type == tokenize.STRING:
                stripped = token.string.lstrip("rRuUbBfF")
                if stripped.startswith(('"""', "'''")):
                    # Triple-quoted docstrings may span many lines; report only
                    # matching excerpts, without treating arbitrary literals as code.
                    for offset, line in enumerate(token.string.splitlines()):
                        yield token.start[0] + offset, line.strip(" \t\"'")
    except (tokenize.TokenError, IndentationError):
        # Source validity is checked by the normal lint/test jobs.
        pass


def other_comments(path: Path, data: str):
    suffix = path.suffix
    multiline = False
    for line_number, raw in enumerate(data.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if suffix == ".sh":
            if line.startswith("#") and not line.startswith("#!"):
                yield line_number, line.lstrip("# ")
            continue
        if suffix in {".html"} and "<!--" in line:
            multiline = True
        if suffix in {".js", ".css", ".ts", ".tsx", ".rs"} and "/*" in line:
            multiline = True
        if multiline:
            yield line_number, line.lstrip("/*-!> ")
        elif line.startswith(("//", "#", "*")):
            yield line_number, line.lstrip("/*# ")
        if multiline and ("*/" in line or "-->" in line):
            multiline = False


def tracked_files():
    out = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"])
    for path_bytes in out.split(b"\0"):
        if not path_bytes:
            continue
        path = Path(path_bytes.decode("utf-8", errors="surrogateescape"))
        if path.suffix not in CODE_SUFFIXES or any(part in SKIP_DIRS for part in path.parts):
            continue
        full = ROOT / path
        if full.is_file() and not full.is_symlink():
            yield path, full


def main() -> int:
    matches = []
    counts = {"files": 0, "comments": 0, "high": 0, "advisory": 0}
    for path, absolute in tracked_files():
        counts["files"] += 1
        data = absolute.read_text(encoding="utf-8", errors="replace")
        lines = python_comments(path, data) if path.suffix == ".py" else other_comments(path, data)
        for line_no, text in lines:
            counts["comments"] += 1
            kind = next((name for name, expression in MARKERS if expression.search(text)), None)
            if kind is None and ADVISORY.search(text):
                kind = "advisory"
            if kind:
                counts["advisory" if kind == "advisory" else "high"] += 1
                matches.append((str(path), line_no, kind, " ".join(text.split())[:180]))
    for path, line, kind, sample in matches[:400]:
        print(f"COMMENT_AUDIT | {kind:21} | {path}:{line} | {sample}")
    print(f"COMMENT_AUDIT_SUMMARY | {counts} | findings={len(matches)}")
    if len(matches) > 400:
        print(f"COMMENT_AUDIT_TRUNCATED | {len(matches) - 400} further results")
    return 1 if counts["high"] else 0


if __name__ == "__main__":
    sys.exit(main())
