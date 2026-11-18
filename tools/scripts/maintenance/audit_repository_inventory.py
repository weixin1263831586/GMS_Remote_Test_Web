#!/usr/bin/env python3
"""Inventory every tracked file and surface verifiable repository drift.

This is a diagnostic scan, not a claim that every execution path was tested.
Hard errors: unparsable first-party Python or missing shared SVG symbols.
Other signals remain review candidates until traced through callers/tests.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root  # noqa: E402

ROOT = find_repo_root()
SPRITE = ROOT / "web/static/icons/ui.svg"
RE_ICON = re.compile(r"ui\.svg#([a-z][a-z0-9-]*)")
RE_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
RE_MARKER = re.compile(r"\b(?:TODO|FIXME|HACK)\b")
SCAN_SUFFIXES = {".py", ".js", ".ts", ".html", ".css", ".md", ".mmd",
                 ".json", ".yml", ".yaml", ".sh", ".svg", ".toml"}
OWNED_PY = ("features/", "foundation/", "bootstrap/", "worker_agent/",
            "workflows/", "agent/gms-remote-test/", "scripts/", "tests/",
            "tools/scripts/")
EXTERNAL = ("http:", "https:", "mailto:", "tel:", "//")


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                            check=True, capture_output=True)
    return sorted(part.decode("utf-8", errors="replace")
                  for part in result.stdout.split(b"\0") if part)


def examine() -> dict:
    names = tracked_files()
    declared = set(re.findall(r'<symbol\s+id="([^"]+)"',
                              SPRITE.read_text(encoding="utf-8")))
    manifest = []
    areas = Counter()
    kinds = Counter()
    duplicates = defaultdict(list)
    findings = []
    icon_usage = Counter()
    for name in names:
        path = ROOT / name
        if not path.is_file():
            findings.append({"kind": "tracked_missing", "path": name})
            continue
        raw = path.read_bytes()
        digest = sha256(raw).hexdigest()
        suffix = path.suffix.lower()
        area = name.split("/", 1)[0]
        areas[area] += 1
        kinds[suffix or "(no extension)"] += 1
        is_text = suffix in SCAN_SUFFIXES and b"\0" not in raw
        record = {"path": name, "area": area, "type": suffix,
                  "bytes": len(raw), "sha256": digest,
                  "scanned_as_text": is_text}
        if is_text:
            source = raw.decode("utf-8", errors="replace")
            record["lines"] = source.count("\n") + int(bool(source) and not source.endswith("\n"))
            if name.startswith(OWNED_PY) and suffix == ".py":
                try:
                    ast.parse(source, filename=name)
                except SyntaxError as exc:
                    findings.append({"kind": "python_syntax", "path": name,
                                     "line": exc.lineno, "detail": exc.msg})
            if suffix in {".html", ".js", ".ts", ".css"}:
                for symbol in RE_ICON.findall(source):
                    icon_usage[symbol] += 1
                    if symbol not in declared:
                        findings.append({"kind": "missing_ui_symbol",
                                         "path": name, "symbol": symbol})
            if suffix == ".md" and (name.startswith("docs/") or name in
                                    {"README.md", "AGENTS.md", "tools/README.md"}):
                for match in RE_LINK.finditer(source):
                    target = match.group(1).split("#", 1)[0].strip()
                    if not target or target.startswith(EXTERNAL) or target.startswith("<"):
                        continue
                    if target.startswith("/"):
                        continue  # Web route, not a repository-relative file.
                    target = unquote(urlsplit(target).path)
                    resolved = (path.parent / target).resolve()
                    if not resolved.is_file() and not resolved.is_dir():
                        findings.append({"kind": "broken_doc_link", "path": name,
                                         "target": target})
            markers = RE_MARKER.findall(source)
            if markers and not name.startswith(("tests/", "plugins/")):
                record["markers"] = len(markers)
            if name.startswith(("features/", "foundation/", "bootstrap/", "worker_agent/",
                                "web/", "scripts/", "tools/scripts/", "workflows/")):
                duplicates[digest].append(name)
        manifest.append(record)

    duplicate_groups = [members for members in duplicates.values()
                        if len(members) > 1]
    # Duplicate bytes alone do not prove a dead file. Inspect the groups manually.
    critical = [issue for issue in findings
                if issue["kind"] in {"tracked_missing", "python_syntax",
                                    "missing_ui_symbol"}]
    return {
        "summary": {
            "tracked_files": len(names),
            "inventory_entries": len(manifest),
            "text_scanned": sum(item["scanned_as_text"] for item in manifest),
            "python_files": kinds[".py"],
            "markdown_files": kinds[".md"],
            "critical": len(critical),
            "findings": len(findings),
            "identical_owned_groups": len(duplicate_groups),
        },
        "areas": dict(sorted(areas.items())),
        "types": dict(sorted(kinds.items())),
        "icon_usage": dict(icon_usage.most_common()),
        "findings": findings,
        "identical_owned_groups": duplicate_groups,
        "manifest": manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    data = examine()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "repository-inventory.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = data["summary"]
    lines = [
        "# Current repository source inventory",
        "",
        "Computed from git-tracked files at the checked-out commit.",
        "A file appearing in this inventory does not establish its necessity or runtime correctness.",
        "",
        "## Totals",
        *[f"- {key}: {value}" for key, value in summary.items()],
        "",
        "## Findings requiring investigation",
        *[f"- {item}" for item in data["findings"][:100]],
        "",
        "## Byte-identical first-party candidate groups",
        *[f"- {'; '.join(group)}" for group in data["identical_owned_groups"][:100]],
        "",
        "## Shared icon usage by symbol",
        *[f"- {icon}: {count}" for icon, count in data["icon_usage"].items()],
    ]
    (args.output / "repository-inventory.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print("Repository scan:", json.dumps(summary, ensure_ascii=False))
    for issue in data["findings"][:25]:
        print("  candidate:", json.dumps(issue, ensure_ascii=False))
    if args.strict and summary["critical"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
