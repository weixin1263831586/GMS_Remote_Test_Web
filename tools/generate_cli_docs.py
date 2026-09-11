#!/usr/bin/env python3
"""Generate docs/cli/command-reference.md from the CLI's own command catalog.

The single source of truth for the gms-rt CLI is the canonical shell entry
point ``agent/gms-remote-test/runtime/gms-remote-test.sh``:

* command names  = shell functions named ``gms-rt-*`` (what
  ``_gms_rt_command_names`` enumerates at runtime);
* usage strings  = the ``_gms_rt_command_usage`` case table;
* summaries      = the ``_gms_rt_command_summary`` case table
  (prefix wildcards like ``gms-rt-burn-*`` are the documented fallbacks).

Parsing the two case tables keeps this generator in lockstep with
``gms-rt-system-commands`` without importing or executing the script.
Never hand-edit docs/cli/command-reference.md — rerun this generator.
``--check`` fails with a diff summary when the generated document drifts
(the contract test tests/contract/test_cli_docs_drift.py runs it in CI).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_SCRIPT = PROJECT_ROOT / "agent/gms-remote-test/runtime/gms-remote-test.sh"
OUTPUT_PATH = PROJECT_ROOT / "docs/cli/command-reference.md"

# case entries binding one or more names to a literal text
_CASE_LITERAL = re.compile(
    r"^\s+(?P<names>[A-Za-z0-9|*-]+)\)\s*printf '%s' '(?P<text>[^']*)' ;;\s*$"
)
# grouped pattern line (names only, printf follows on the next line)
_CASE_GROUP = re.compile(r"^\s+(?P<names>[A-Za-z0-9|*-]+)\)\s*$")
# grouped printf line: "$1" or "$1 <devices>"
_CASE_GROUP_PRINTF = re.compile(r"^\s*printf '%s' \"\$1(?P<suffix>[^\"]*)\" ;;\s*$")
_FUNCTION_DEF = re.compile(r"^(?P<name>gms-rt-[a-z0-9-]+)\(\)\s*\{", re.MULTILINE)

HEADER = """# gms-rt 命令参考

> 此文件由 `tools/generate_cli_docs.py` 生成，请勿手工编辑。
> 真源：`agent/gms-remote-test/runtime/gms-remote-test.sh`
> （`_gms_rt_command_usage` / `_gms_rt_command_summary` 命令目录，与
> `gms-rt-system-commands` 的机器可读清单同源）。

| 命令 | 用途 | 用法 |
|---|---|---|
"""


def _function_body(script: str, function_name: str) -> str:
    """Return the body of a shell function (up to the closing brace line)."""
    match = re.search(rf"^{re.escape(function_name)}\(\)\s*\{{\s*$", script, re.MULTILINE)
    if not match:
        raise SystemExit(f"function {function_name}() not found in {CLI_SCRIPT}")
    lines: list[str] = []
    for line in script[match.end():].splitlines():
        if line.strip() == "}":
            return "\n".join(lines)
        lines.append(line)
    raise SystemExit(f"function {function_name}() is not terminated in {CLI_SCRIPT}")


def _parse_case_table(body: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse a case table into (exact -> text, wildcard-prefix -> text)."""
    exact: dict[str, str] = {}
    wildcard: dict[str, str] = {}
    pending_names: list[str] | None = None
    for line in body.splitlines():
        group = _CASE_GROUP.match(line)
        if group:
            pending_names = group.group("names").split("|")
            continue
        if pending_names is not None:
            printf = _CASE_GROUP_PRINTF.match(line)
            if printf:
                for name in pending_names:
                    if name.endswith("*"):
                        wildcard[name[:-1]] = "$1" + printf.group("suffix")
                    else:
                        exact[name] = "$1" + printf.group("suffix")
                pending_names = None
                continue
        literal = _CASE_LITERAL.match(line)
        if literal:
            for name in literal.group("names").split("|"):
                if name.endswith("*"):
                    wildcard[name[:-1]] = literal.group("text")
                else:
                    exact[name] = literal.group("text")
            pending_names = None
    return exact, wildcard


def _resolve(table: dict[str, str], wildcards: dict[str, str], command: str) -> str:
    if command in table:
        return table[command]
    for prefix, text in sorted(wildcards.items(), key=lambda kv: (-len(kv[0]), kv[0])):
        if command.startswith(prefix):
            return text
    return ""


def _escape(cell: str) -> str:
    return cell.replace("|", "\\|")


def build_document() -> str:
    script = CLI_SCRIPT.read_text(encoding="utf-8")
    commands = sorted(set(_FUNCTION_DEF.findall(script)))
    if not commands:
        raise SystemExit(f"no gms-rt-* functions found in {CLI_SCRIPT}")

    usage_exact, usage_wild = _parse_case_table(_function_body(script, "_gms_rt_command_usage"))
    summary_exact, summary_wild = _parse_case_table(_function_body(script, "_gms_rt_command_summary"))

    rows = []
    missing_summary = []
    for command in commands:
        usage = _resolve(usage_exact, usage_wild, command)
        summary = _resolve(summary_exact, summary_wild, command)
        if not summary:
            missing_summary.append(command)
        rows.append(f"| `{command}` | {_escape(summary)} | {_escape(usage)} |")

    doc = HEADER + "\n".join(rows) + "\n"
    if missing_summary:
        print(
            "warning: commands without a catalog summary: " + ", ".join(missing_summary),
            file=sys.stderr,
        )
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail with a diff summary instead of writing when the document is stale",
    )
    args = parser.parse_args()

    document = build_document()
    if not args.check:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(document, encoding="utf-8")
        print(f"wrote {OUTPUT_PATH} ({len(document.splitlines())} lines)")
        return 0

    if not OUTPUT_PATH.exists():
        print(f"DRIFT: {OUTPUT_PATH} does not exist", file=sys.stderr)
        return 1
    current = OUTPUT_PATH.read_text(encoding="utf-8")
    if current == document:
        print("cli docs up to date")
        return 0
    current_lines = current.splitlines()
    generated_lines = document.splitlines()
    diff: list[str] = []
    for index in range(max(len(current_lines), len(generated_lines))):
        old = current_lines[index] if index < len(current_lines) else "<missing>"
        new = generated_lines[index] if index < len(generated_lines) else "<missing>"
        if old != new:
            diff.append(f"  line {index + 1}:\n    - {old}\n    + {new}")
        if len(diff) >= 10:
            break
    print("DRIFT: docs/cli/command-reference.md is stale; rerun "
          "python3 tools/generate_cli_docs.py to update it.\n" + "\n".join(diff),
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
