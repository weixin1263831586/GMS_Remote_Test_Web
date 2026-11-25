#!/usr/bin/env python3
"""Inventory every tracked file and surface verifiable repository drift.

This is a diagnostic scan, not a claim that every execution path was tested.
Hard errors: unparsable first-party Python, missing shared SVG symbols, broken repository documentation links or missing first-party Python import targets.
Other signals remain review candidates until traced through callers/tests.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from hashlib import sha256
from importlib.util import resolve_name
from pathlib import Path
from urllib.parse import unquote, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


ROOT = find_repo_root()
sys.path.insert(0, str(ROOT))
from tests.architecture._import_refs import literal_dynamic_targets  # noqa: E402
from tests.architecture._size_ratchet import (  # noqa: E402
    BACKEND_SOURCE_SUFFIXES,
    FRONTEND_SOURCE_SUFFIXES,
    MAX_SOURCE_LINES,
    THIRD_PARTY_PREFIXES,
)


SPRITE = ROOT / "web/static/icons/ui.svg"
RE_ICON = re.compile(r"ui\.svg#([a-z][a-z0-9-]*)")
RE_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
RE_MARKER = re.compile(r"\b(?:TODO|FIXME|HACK)\b")
SCAN_SUFFIXES = {".py", ".js", ".ts", ".html", ".css", ".md", ".mmd",
                 ".json", ".yml", ".yaml", ".sh", ".svg", ".toml",
                 *BACKEND_SOURCE_SUFFIXES, *FRONTEND_SOURCE_SUFFIXES}
SOURCE_SUFFIXES = BACKEND_SOURCE_SUFFIXES | FRONTEND_SOURCE_SUFFIXES
RE_RETIRED_SDK = re.compile(r"features(?:\.system\.source_|/system/source_)")
RE_SIZE_SPLIT = re.compile(
    r"(?:行数上限|行数门禁|architecture\s+size limit|below its architecture|api\.py size budget)",
    re.IGNORECASE,
)

OWNED_PY = ("features/", "foundation/", "bootstrap/", "worker/",
            "agent/gms-remote-test/", "scripts/", "tests/",
            "tools/scripts/", "plugins/codesearch/")
# Retired package roots remain in the import checker to catch stale references.
LOCAL_IMPORT_ROOTS = {"features", "foundation", "bootstrap", "worker", "workflows", "scripts_local", "tests"}
EXTERNAL = ("http:", "https:", "mailto:", "tel:", "//")


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                            check=True, capture_output=True)
    return sorted(part.decode("utf-8", errors="replace")
                  for part in result.stdout.split(b"\0") if part)


def repository_modules(names: list[str]) -> set[str]:
    """Index first-party Python modules and their namespace packages without importing them."""
    modules = set()
    for name in names:
        path = Path(name)
        if path.suffix != ".py" or path.parts[0] not in LOCAL_IMPORT_ROOTS:
            continue
        parts = path.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.update(".".join(parts[:index]) for index in range(1, len(parts) + 1))
    return modules


def module_exports(names: list[str]) -> dict[str, set[str]]:
    """Index statically declared exports, skipping dynamic or wildcard modules."""
    result = {}
    for name in names:
        path = Path(name)
        if path.suffix != ".py" or path.parts[0] not in LOCAL_IMPORT_ROOTS:
            continue
        try:
            syntax = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=name)
        except (OSError, SyntaxError, UnicodeError):
            continue
        nodes = list(ast.walk(syntax))
        if any(
            (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__")
            or (isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names))
            or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"exec", "globals", "setattr"})
            for node in nodes
        ):
            continue
        exports = {"__name__", "__path__", "__file__", "__package__", "__all__",
                   "__spec__", "__loader__", "__doc__", "__dict__", "__builtins__", "__annotations__"}
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                exports.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                exports.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                exports.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        parts = path.parent.parts if path.name == "__init__.py" else path.with_suffix("").parts
        result[".".join(parts)] = exports
    return result


def missing_local_imports(name: str, tree: ast.AST, modules: set[str],
                          exports: dict[str, set[str]]) -> list[dict]:
    """Check explicit local import targets, including deferred and relative imports."""
    findings = []
    package = ".".join(Path(name).parent.parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                if not package or package.split(".")[0] not in LOCAL_IMPORT_ROOTS:
                    continue
                try:
                    target = resolve_name("." * node.level + target, package)
                except ImportError:
                    findings.append({"kind": "missing_local_import", "path": name,
                                     "line": node.lineno, "target": "." * node.level + target})
                    continue
            targets = [target]
            if target in exports:
                targets.extend(f"{target}.{alias.name}" for alias in node.names
                               if alias.name != "*" and alias.name not in exports[target])
        else:
            continue
        for target in targets:
            if target.split(".")[0] in LOCAL_IMPORT_ROOTS and target not in modules:
                findings.append({"kind": "missing_local_import", "path": name,
                                 "line": node.lineno, "target": target})
    # Negative import tests intentionally exercise missing modules; application
    # literal dynamic references, not test expectations, are the contract here.
    if not name.startswith("tests/") and "/tests/" not in name:
        for node, target in literal_dynamic_targets(tree, package):
            if target.split(".")[0] in LOCAL_IMPORT_ROOTS and target not in modules:
                findings.append({"kind": "missing_local_import", "path": name,
                                 "line": node.lineno, "target": target})
    return findings


def examine() -> dict:
    names = tracked_files()
    modules = repository_modules(names)
    exports = module_exports(names)
    declared = set(re.findall(r'<symbol\s+id="([^"]+)"',
                              SPRITE.read_text(encoding="utf-8")))
    manifest = []
    areas = Counter()
    kinds = Counter()
    duplicates = defaultdict(list)
    findings = []
    icon_usage = Counter()
    source_groups = Counter()
    oversized = []
    retired_sdk = []
    size_split_candidates = []
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
        is_source = not name.startswith(THIRD_PARTY_PREFIXES) and (
            suffix in SOURCE_SUFFIXES or (not suffix and raw.startswith(b"#!"))
        )
        is_text = (suffix in SCAN_SUFFIXES or is_source) and b"\0" not in raw
        record = {"path": name, "area": area, "type": suffix,
                  "bytes": len(raw), "sha256": digest,
                  "scanned_as_text": is_text}
        if is_text:
            source = raw.decode("utf-8", errors="replace")
            record["lines"] = len(source.splitlines())
            if is_source:
                parts = Path(name).parts
                domain = "/".join(parts[:2]) if parts[0] == "features" else parts[0]
                source_groups[domain] += 1
                if record["lines"] > MAX_SOURCE_LINES:
                    item = {"kind": "source_line_limit", "path": name, "lines": record["lines"]}
                    oversized.append(item)
                    findings.append(item)
                if name.startswith(OWNED_PY) and "/tests/" not in name and not name.startswith("tests/"):
                    if name != "tools/scripts/maintenance/audit_repository_inventory.py" and RE_RETIRED_SDK.search(source):
                        retired_sdk.append(name)
                        findings.append({"kind": "retired_sdk_reference", "path": name})
                    if suffix == ".py" and RE_SIZE_SPLIT.search(source):
                        size_split_candidates.append(name)
            if suffix == ".py" and ("/" not in name or name.startswith(OWNED_PY)):
                try:
                    syntax_tree = ast.parse(source, filename=name)
                    findings.extend(missing_local_imports(name, syntax_tree, modules, exports))
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
            if name.startswith(("features/", "foundation/", "bootstrap/", "worker/",
                                "web/", "scripts/", "tools/scripts/")):
                duplicates[digest].append(name)
        manifest.append(record)

    duplicate_groups = [members for members in duplicates.values()
                        if len(members) > 1]
    # Duplicate bytes alone do not prove a dead file. Inspect the groups manually.
    critical = [issue for issue in findings
                if issue["kind"] in {"tracked_missing", "python_syntax",
                                    "missing_ui_symbol", "broken_doc_link", "missing_local_import",
                                     "source_line_limit", "retired_sdk_reference"}]
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
        "source_layout": {
            "groups": dict(sorted(source_groups.items())),
            "source_files": sum(source_groups.values()),
            "over_limit": oversized,
            "retired_sdk_references": sorted(retired_sdk),
            "size_split_candidates": sorted(size_split_candidates),
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
    print("LAYOUT_AUDIT=" + json.dumps(data["source_layout"], ensure_ascii=True), flush=True)
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
