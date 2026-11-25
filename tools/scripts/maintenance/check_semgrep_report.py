"""Validate a strict Semgrep report and narrowly reviewed execution boundaries."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


REQUIRED_TARGETS = {
    "plugins/codesearch/scripts/codesearch.py",
    "plugins/codesearch/scripts/mcp_server.py",
    "tools/scripts/agent/release.py",
    "features/build/executor.py",
    "foundation/processes.py",
    "features/cluster/deployment/api.py",
    "features/system/host/terminal/output.py",
}
BLOCKING = {"ERROR", "HIGH", "CRITICAL"}

# Exact file identity, line and rule bind each exception to its reviewed implementation.
# Any source edit requires renewed review; findings remain visible in the raw report.
APPROVED_EXECUTION = {
    ("features/cluster/deployment/api.py", 146,
     "python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args"):
        ("fca5dda46f2d10de71603fafd60a562891e7c5b3",
         "Elevated-admin software setup selects a fixed executable and validated deployment unit."),
    ("features/system/host/terminal/output.py", 55,
     "python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args"):
        ("67bc7e69bdd0084dfb6f4c493e249e7c3556dc34",
         "Authorized interactive PTY uses an argv list and a local environment."),
    ("tools/scripts/agent/dev.py", 84,
     "python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args"):
        ("043c73450f1417574105f873aa826cb6312a41ec",
         "Local developer CLI re-executes the current Python interpreter with a fixed package entrypoint."),
}
# Semgrep's shell parser cannot parse the upstream launcher loop. Independently
# verify the unchanged upstream blobs and POSIX shell syntax, not a directory exclusion.
VENDOR_PARSER_LIMITS = {
    "tools/jadx/bin/jadx": "42ece41befa9e695f1b03586eb6a5cae645b1083",
    "tools/jadx/bin/jadx-gui": "57efa84e46cd51953cbe4fb6b30b6d773a173c7a",
}


def blob_matches(path: str, expected: str) -> bool:
    result = subprocess.run(
        ["git", "hash-object", "--", path], capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == expected


def reviewed_parser_limit(issue: dict) -> bool:
    path = issue.get("path")
    kind = issue.get("type")
    if not isinstance(kind, list) or not kind or kind[0] != "PartialParsing":
        return False
    if path not in VENDOR_PARSER_LIMITS or issue.get("code") != 3:
        return False
    expected_message = f"Syntax error at line {path}:175:\n `for i` was unexpected"
    if issue.get("message") != expected_message or not blob_matches(path, VENDOR_PARSER_LIMITS[path]):
        return False
    return subprocess.run(["sh", "-n", path], check=False).returncode == 0


def review_report(report: dict, scan_exit_code: int) -> int:
    scan_errors = report["errors"]
    results = report["results"]
    scanned = set(report["paths"]["scanned"])
    if not isinstance(scan_errors, list) or not isinstance(results, list):
        raise ValueError("Invalid Semgrep report")
    failed = False
    parser_limits = 0
    for issue in scan_errors:
        if reviewed_parser_limit(issue):
            parser_limits += 1
            print(f"::warning::Verified upstream parser limitation: {issue['path']}")
        else:
            failed = True
            print(f"::error::Semgrep scan error: {issue}")
    if scan_exit_code != 0 and not (
        scan_exit_code == 3 and parser_limits and parser_limits == len(scan_errors)
    ):
        failed = True
        print(f"::error::Unexpected Semgrep exit code: {scan_exit_code}")
    if missing := REQUIRED_TARGETS - scanned:
        failed = True
        print(f"::error::Semgrep required targets not scanned: {sorted(missing)}")
    blocking = 0
    reviewed = 0
    for finding in results:
        if finding["extra"]["severity"] not in BLOCKING:
            continue
        path, line, rule = finding["path"], finding["start"]["line"], finding["check_id"]
        approved = APPROVED_EXECUTION.get((path, line, rule))
        if approved and blob_matches(path, approved[0]):
            reviewed += 1
            print(f"::notice file={path},line={line}::{rule}: {approved[1]}")
            continue
        blocking += 1
        print(f"::error file={path},line={line}::{rule}")
    print(f"Semgrep: {len(results)} findings; {blocking} blocking; {reviewed} reviewed execution boundaries; "
          f"{parser_limits} verified vendor parser limitations")
    return int(failed or bool(blocking))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--scan-exit-code", type=int, required=True)
    args = parser.parse_args()
    return review_report(json.loads(args.report.read_text(encoding="utf-8")), args.scan_exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
