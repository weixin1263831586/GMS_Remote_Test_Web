#!/usr/bin/env python3
"""Shrink-only size-budget maintenance for the architecture size gates.

The file-size ratchet (ADR 0002 / docs/development.md) means budgets may only
SHRINK after a real split: keeping an old peak budget after extracting a
module re-opens the debt it just closed. Ceilings live in independent
persisted baselines (``tests/architecture/baselines/*.json``); the ratchet
gate (``_size_ratchet.assert_no_ceiling_raises``) compares them against the
previous commit, so raising a ceiling requires a reviewed waiver — an
automated or sneaky bump can no longer keep the gate green.

This tool tightens every ceiling down to the file's CURRENT size — never up:

    python tools/scripts/maintenance/update_size_baseline.py --shrink-only          # apply
    python tools/scripts/maintenance/update_size_baseline.py --shrink-only --dry-run

Entries whose file GREW beyond its ceiling are reported but left untouched:
growth means either a regression the gate must keep failing on, or a
deliberate raise that needs a waiver entry (reason + expiry) reviewed in the
commit, not an automated bump. Files missing from the baseline are policed by
the 600-line / 50 KB default limits in the owning tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


ROOT = find_repo_root()
BASELINES = (
    ROOT / "tests" / "architecture" / "baselines" / "file_size.json",
    ROOT / "tests" / "architecture" / "baselines" / "frontend_size.json",
)


def _shrink_baseline(path: Path, *, dry_run: bool) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    ceilings = data.get("ceilings") or {}
    shrunk = 0
    lines: list[str] = []
    for relative in sorted(ceilings):
        source = ROOT / relative
        if not source.is_file():
            continue
        is_bytes = data.get("unit") == "bytes"
        actual = (
            source.stat().st_size
            if is_bytes
            else len(source.read_text(encoding="utf-8").splitlines())
        )
        registered = int(ceilings[relative])
        if actual < registered:
            ceilings[relative] = actual
            shrunk += 1
            lines.append(f"shrink  {relative}: {registered} -> {actual}")
        elif actual > registered:
            lines.append(
                f"GREW    {relative}: ceiling {registered}, actual {actual} "
                "(left unchanged; raise needs a reviewed waiver)"
            )
    if dry_run:
        print(f"--- {path.relative_to(ROOT)} (dry run)")
    elif shrunk:
        data["ceilings"] = ceilings
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    for line in lines:
        print(line)
    if not lines:
        print(f"{path.relative_to(ROOT)}: nothing to tighten")
    return shrunk


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shrink-only", action="store_true", required=True,
        help="only lower ceilings to the current size; never raise",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = parser.parse_args(argv)

    total = 0
    for baseline in BASELINES:
        if baseline.is_file():
            total += _shrink_baseline(baseline, dry_run=args.dry_run)
        else:
            print(f"[WARN] missing baseline: {baseline.relative_to(ROOT)}")
    if not args.dry_run and total:
        print(f"{total} ceiling(s) tightened across baselines")
    elif not args.dry_run:
        print("nothing to shrink; ceilings already tight")
    # Growth is informational: the ratchet + runtime tests still fail on
    # files above ceiling, which is exactly the desired gate behaviour.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
