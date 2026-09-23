#!/usr/bin/env python3
"""Regenerate `artifact_sha256` blocks inside tools/ provenance manifests.

统一 provenance schema（tests/architecture/test_tool_provenance.py）要求
目录级清单（jadx 等）的 ``artifact_sha256`` 覆盖全部已跟踪分发工件，
``tools/utilities.provenance.json`` 的每条 sha256 与实际文件一致。
替换/升级工具后，不要手算哈希，用本脚本重算并写回：

    python tools/scripts/maintenance/generate_tool_provenance_sha256.py tools/jadx/jadx.provenance.json
    python tools/scripts/maintenance/generate_tool_provenance_sha256.py tools/utilities.provenance.json
    python tools/scripts/maintenance/generate_tool_provenance_sha256.py --all        # 上述全部
    python tools/scripts/maintenance/generate_tool_provenance_sha256.py --dry-run <path...>

写回策略（2 空格缩进 JSON，键序保持文件原序）：

* 目录级清单：重建 ``artifact_sha256``（git ls-files 覆盖目录内全部
  除清单自身外的已跟踪文件）；
* utilities 清单：仅刷新各条目 ``sha256``（文件列表即清单条目本身）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


ROOT = find_repo_root()
DEFAULT_TARGETS = (
    ROOT / "tools" / "jadx" / "jadx.provenance.json",
    ROOT / "tools" / "utilities.provenance.json",
)


def _tracked_files(directory: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", directory.as_posix() + "/"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [
        ROOT / rel
        for rel in out
        if not rel.endswith(".provenance.json") and (ROOT / rel).is_file()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def refresh_directory_manifest(manifest: Path, *, dry_run: bool) -> list[str]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    directory = manifest.parent
    mapping = {p.relative_to(ROOT).as_posix(): _sha256(p) for p in _tracked_files(directory)}
    data["artifact_sha256"] = dict(sorted(mapping.items()))
    changed = sum(
        1 for rel, digest in mapping.items()
        if (data.get("artifact_sha256") or {}).get(rel) != digest
    )
    if not dry_run:
        _rewrite_json(manifest, data)
    return [f"{manifest.relative_to(ROOT)}: refreshed {len(mapping)} entries ({changed} changed)"]


def refresh_utilities_manifest(manifest: Path, *, dry_run: bool) -> list[str]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    utilities = data.get("utilities")
    if not isinstance(utilities, dict) or not utilities:
        return [f"{manifest.relative_to(ROOT)}: no utilities section, skipped"]
    changed = 0
    for rel, entry in sorted(utilities.items()):
        path = ROOT / rel
        if not path.is_file():
            print(f"[WARN] missing artifact for entry {rel}", file=sys.stderr)
            continue
        digest = _sha256(path)
        if entry.get("sha256") != digest:
            entry["sha256"] = digest
            changed += 1
    if not dry_run and changed:
        _rewrite_json(manifest, data)
    return [f"{manifest.relative_to(ROOT)}: {len(utilities)} entries ({changed} changed)"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", nargs="*", type=Path, help="provenance manifest paths")
    parser.add_argument("--all", action="store_true", help="refresh all default manifests")
    parser.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = parser.parse_args(argv)

    if args.all:
        targets = list(DEFAULT_TARGETS)
    elif args.manifests:
        targets = []
        for raw in args.manifests:
            path = raw if raw.is_absolute() else ROOT / raw
            if not path.is_file():
                parser.error(f"manifest not found: {raw}")
            targets.append(path)
    else:
        parser.error("provide manifest paths or --all")

    for target in targets:
        for line in (
            refresh_directory_manifest(target, dry_run=args.dry_run)
            if target.name != "utilities.provenance.json"
            else refresh_utilities_manifest(target, dry_run=args.dry_run)
        ):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
