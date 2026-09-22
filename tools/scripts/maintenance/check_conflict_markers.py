#!/usr/bin/env python3
"""Reject unresolved Git merge conflict markers in tracked text files.

Commit 7dbf641 shipped ``<<<<<<< Updated upstream`` blocks in
``web/shell/shell.html`` and ``web/static/js/shell/shell-main.js`` to main;
9b470c2 cleaned them up afterwards. Agent-driven development resolves merges
at file granularity, so this cheap gate keeps a partially resolved file out
of main instead of relying on reviewers to spot marker lines.

Marker syntax checked (exact Git forms, so decorative separators such as
``# ===== section =====`` or setext underline rows do not false-positive):

    <<<<<<< <label>      conflict start (7 '<' followed by a label)
    =======              separator    (exactly 7 '=' on its own line)
    >>>>>>> <label>      conflict end (7 '>' followed by a label)

Usage (CI lint job and locally):
    python tools/scripts/maintenance/check_conflict_markers.py [paths...]
"""

from __future__ import annotations

import re
import subprocess
import sys


# Git always writes the marker column at position 0, exactly 7 characters,
# followed by a space + label for the opening/closing rows.
START_MARKER = re.compile(r"^<{7}( .+)?$")
SEPARATOR_MARKER = re.compile(r"^={7}$")
END_MARKER = re.compile(r"^>{7}( .+)?$")


def tracked_files(paths: list[str]) -> list[str]:
    cmd = ["git", "ls-files", "--", *(paths or ["."])]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        print(out.stderr.strip(), file=sys.stderr)
        raise SystemExit(out.returncode)
    return [line for line in out.stdout.splitlines() if line]


def file_markers(path: str) -> list[tuple[int, str]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
    except OSError:
        return []
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        content = line.rstrip("\r\n")
        if (
            START_MARKER.match(content)
            or SEPARATOR_MARKER.match(content)
            or END_MARKER.match(content)
        ):
            hits.append((number, content))
    return hits


def main(argv: list[str]) -> int:
    offenders: list[str] = []
    for path in tracked_files(argv):
        for number, content in file_markers(path):
            offenders.append(f"{path}:{number}: {content[:120]}")
    if offenders:
        print("Unresolved merge conflict markers found:", file=sys.stderr)
        for entry in offenders:
            print(f"  {entry}", file=sys.stderr)
        print(
            "\nResolve the conflicts and remove the markers, or run "
            "`git checkout --theirs/--ours` deliberately.",
            file=sys.stderr,
        )
        return 1
    print("No merge conflict markers found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
