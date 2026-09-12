#!/usr/bin/env python3
"""Shrink-only size-budget maintenance for tests/architecture/test_file_size_rules.py.

The file-size ratchet (ADR 0002 / docs/development.md) means budgets may only
SHRINK after a real split: keeping an old peak budget after extracting a
module re-opens the debt it just closed. This tool syncs every listed budget
down to the file's CURRENT line count — never up — so a refactor that made
files smaller immediately tightens the gate:

    python tools/update_size_baseline.py --shrink-only          # apply
    python tools/update_size_baseline.py --shrink-only --dry-run

Files that GREW beyond their budget are reported but left untouched: growth
means either a regression the gate must keep failing on, or a deliberate
budget raise that belongs in a reviewed commit, not an automated bump.
Modules not present in the budget table are ignored (the architecture test
already defaults them to the 600-line new-module limit).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = ROOT / "tests" / "architecture" / "test_file_size_rules.py"

# Matches entries like  'features/x.py': 123,  # optional comment
ENTRY_RE = re.compile(r"(?m)^(\s+'(?P<path>[^']+)': )(?P<limit>\d+)(?P<rest>,.*|,)$")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shrink-only", action="store_true", required=True,
        help="only lower budgets to the current line count; never raise",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = parser.parse_args(argv)

    text = RULES_FILE.read_text(encoding="utf-8")
    shrunk: list[tuple[str, int, int]] = []
    grown: list[tuple[str, int, int]] = []

    def replace(match: re.Match) -> str:
        path = match.group("path")
        limit = int(match.group("limit"))
        source = ROOT / path
        if not source.is_file():
            return match.group(0)
        actual = len(source.read_text(encoding="utf-8").splitlines())
        if actual < limit:
            shrunk.append((path, limit, actual))
            return f"{match.group(1)}{actual}{match.group('rest')}"
        if actual > limit:
            grown.append((path, limit, actual))
        return match.group(0)

    new_text = ENTRY_RE.sub(replace, text)

    for path, old, new in shrunk:
        print(f"shrink  {path}: {old} -> {new}")
    for path, old, new in grown:
        print(f"GREW    {path}: budget {old}, actual {new} (left unchanged)")

    if args.dry_run:
        print("(dry run: nothing written)")
        return 0
    if shrunk:
        RULES_FILE.write_text(new_text, encoding="utf-8")
        print(f"{len(shrunk)} budget(s) tightened in {RULES_FILE.relative_to(ROOT)}")
    else:
        print("nothing to shrink; budgets already tight")
    # Growth is informational: the architecture test itself still fails on
    # files above budget, which is exactly the desired gate behaviour.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
