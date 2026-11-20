#!/usr/bin/env python3
"""Read-only physical-host inventory of GMS runtime artifacts.

Only aggregates names of *known* top-level roots. Never prints per-file names,
reads file contents, follows symlinks, removes files, or changes app state.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from _live_runtime_roots import active_disk_root_environment


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return str(n)


def make_stat() -> dict:
    return {"bytes": 0, "files": 0, "dirs": 0,
            "older_7d_bytes": 0, "older_30d_bytes": 0,
            "older_90d_bytes": 0}


# Only fixed labels may reach CI logs and exported JSON.
APK_EXTENSIONS = frozenset({".apk", ".dex", ".java", ".class", ".xml", ".so", ".jar", ".png", ".json"})

_DATA_GROUPS = {
    "uploads": {"gms_firmware_uploads", "gms_uploads"},
    "cluster": {"artifacts", "artifact-uploads", "transfers", "firmware",
                "jobs", "suites", "workers", "runs"},
    "automation": {"artifact-stage"},
    "assistant": {"agent_transcripts"},
    "redmine": {"attachments", "docs", "evidence"},
    "apk_uploads": set(), "reports": set(), "devices_console": set(),
    "config_explorer_cache": set(),
}


def category_for(relative: tuple[str, ...], prefix: str) -> str:
    if prefix not in ("data", "custom-data"):
        return prefix
    if not relative:
        return "data 根目录"
    top = relative[0]
    if top not in _DATA_GROUPS:
        return "其他"
    if relative[0:2] == ("redmine", "by_user"):
        if len(relative) < 4:
            return "redmine/by_user（根）"
        section = relative[3]
        return "redmine/by_user/*/" + (section if section in _DATA_GROUPS[top] else "其他")
    if len(relative) > 1 and _DATA_GROUPS[top]:
        return top + "/" + (relative[1] if relative[1] in _DATA_GROUPS[top] else "其他")
    return top


def scan_root(root: Path, label: str, stats: dict, top_stats: dict,
              extensions: dict, budget: dict, now: float) -> dict:
    report = {"root": label, "present": False, "complete": True, "reason": "",
              "bytes": 0, "files": 0, "dirs": 0, "visited": 0}
    if root.is_symlink():
        report.update(complete=False, reason="根目录是符号链接，未进入")
        return report
    try:
        root.lstat()
    except FileNotFoundError:
        return report
    except OSError:
        report.update(complete=False, reason="根目录不可读取")
        return report
    report["present"] = True
    stack = [(root, ())]
    while stack:
        if budget["visited"] >= budget["limit"] or time.monotonic() > budget["deadline"]:
            report.update(complete=False, reason="达到总文件数/时间上限")
            break
        path, rel = stack.pop()
        try:
            st = path.lstat()
        except (OSError, ValueError):
            report.update(complete=False, reason="部分条目不可访问")
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        category = category_for(rel, label)
        if stat.S_ISREG(st.st_mode):
            report["files"] += 1
            report["bytes"] += st.st_size
            record = stats[category]
            record["files"] += 1
            record["bytes"] += st.st_size
            bucket = category_for(rel[:1], label) if label in ("data", "custom-data") else "(all)"
            root_bucket = top_stats[label + "/" + bucket]
            if label in ("data", "custom-data") and rel and rel[0] == "apk_uploads":
                suffix = path.suffix.lower()
                if suffix not in APK_EXTENSIONS:
                    suffix = "其他" if suffix else "(无后缀)"
                extensions[suffix]["bytes"] += st.st_size
                extensions[suffix]["files"] += 1
            root_bucket["files"] += 1
            root_bucket["bytes"] += st.st_size
            age = now - st.st_mtime
            for days, key in ((7, "older_7d_bytes"), (30, "older_30d_bytes"),
                              (90, "older_90d_bytes")):
                if age > days * 86400:
                    record[key] += st.st_size
        elif stat.S_ISDIR(st.st_mode):
            report["dirs"] += 1
            stats[category]["dirs"] += 1
            try:
                with os.scandir(path) as handle:
                    for entry in handle:
                        if entry.is_symlink():
                            continue
                        stack.append((Path(entry.path), (*rel, entry.name)))
                        if len(stack) > 500_000:
                            report.update(complete=False, reason="遍历队列上限")
                            break
            except OSError:
                report.update(complete=False, reason="部分目录不可读取")
        budget["visited"] += 1
        report["visited"] += 1
        if report["reason"] == "遍历队列上限":
            break
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-entries", type=int, default=3_000_000)
    parser.add_argument("--max-seconds", type=int, default=150)
    args = parser.parse_args()
    project = Path(args.host_root).expanduser().absolute()
    if not (project / "actions-runner" / ".runner").is_file():
        raise SystemExit("非指定 GMS actions-runner 宿主，拒绝扫描")
    os.umask(0o077)
    start = time.monotonic()
    now = time.time()
    budget = {"limit": args.max_entries, "visited": 0,
              "deadline": start + args.max_seconds}
    stats = defaultdict(make_stat)
    top_stats = defaultdict(make_stat)
    extensions = defaultdict(lambda: {"bytes": 0, "files": 0})
    runtime_env, configuration_scope = active_disk_root_environment()
    roots = [
        ("data", project / "data"),
        ("host-logs", project / "logs"),
        ("host-reports", project / "reports"),
        ("host-artifacts", project / "artifacts"),
        ("host-uploads", project / "uploads"),
        ("host-backups", project / "backups"),
        ("host-apk-uploads", project / "apk_uploads"),
        ("host-cache", project / "cache"),
        ("host-tmp", project / "tmp"),
        ("test-logs", Path(runtime_env.get("GMS_TEST_LOG_ROOT", "/tmp/test-logs"))),
        ("worker-jobs", Path.home() / "gms-worker-data" / "jobs"),
    ]
    if runtime_env.get("GMS_DATA_ROOT"):
        custom = Path(runtime_env["GMS_DATA_ROOT"]).expanduser().absolute()
        roots.insert(0, ("custom-data", custom))
        # Only the configured active data root should feed category totals.
        # Otherwise a stale default data/ tree would inflate comparison data.
        if custom != (project / "data").absolute():
            roots = [(label, root) for label, root in roots if label != "data"]
    if runtime_env.get("GMS_UPLOAD_DIR"):
        roots.append(("custom-uploads", Path(runtime_env["GMS_UPLOAD_DIR"])))
    # Inspect only well-known temporary prefixes, never the whole /tmp.
    temp = Path("/tmp")
    try:
        matches = sorted((p for p in temp.iterdir()
                          if p.name.startswith(("gms-report-download-", "report_agent_"))
                          and not p.is_symlink()), key=lambda p: p.name)
    except OSError:
        matches = []
    # Avoid unbounded roots and filenames in the public log.
    for path in matches[:100]:
        roots.append(("tmp-report-export", path))
    result_roots = []
    unique = set()
    for label, root in roots:
        try:
            identity = str(root.resolve())
        except (OSError, RuntimeError):
            identity = str(root)
        if identity in unique:
            continue
        unique.add(identity)
        result_roots.append(scan_root(root, label, stats, top_stats, extensions, budget, now))
    try:
        filesystem = os.statvfs(project)
        disk = {"total": filesystem.f_frsize * filesystem.f_blocks,
                "free": filesystem.f_frsize * filesystem.f_bavail}
    except OSError:
        disk = {"total": None, "free": None}
    categories = [
        {"name": name, **values} for name, values in stats.items()
    ]
    categories.sort(key=lambda row: row["bytes"], reverse=True)
    top_directories = [
        {"name": name, **values} for name, values in top_stats.items()
    ]
    top_directories.sort(key=lambda row: row["bytes"], reverse=True)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": os.environ.get("RUNNER_NAME", "unknown"),
        "read_only": True,
        "configuration_scope": configuration_scope,
        "elapsed_seconds": round(time.monotonic() - start, 2),
        "entries_visited": budget["visited"],
        "root_reports": result_roots,
        "categories": categories,
        "top_directories": top_directories[:50],
        "apk_extensions": sorted(
            ({"extension": name, **stat} for name, stat in extensions.items()),
            key=lambda row: row["bytes"], reverse=True,
        )[:30],
        "disk": disk,
        "coverage_notes": [
            "只扫描 Controller 主机上固定的 GMS 数据和临时目录",
            "不递归扫描其他主机的 Worker、Windows Source 或远程测试套件目录",
            "文件年龄是观察数据，不等于可删除判定",
            "不读取文件内容、不输出用户文件名、不跟随符号链接",
        ],
    }
    output = Path(args.output)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("=== GMS LIVE READ-ONLY ARTIFACT SCAN ===")
    print("RUNNER:", result["host"], "elapsed:", result["elapsed_seconds"],
          "s entries:", result["entries_visited"])
    print("HOST DISK: free", human(disk["free"]) if disk["free"] is not None else "unknown",
          "total", human(disk["total"]) if disk["total"] is not None else "unknown")
    print("ROOTS:")
    for entry in result_roots:
        print(" ", entry["root"], "present=", entry["present"],
              "complete=", entry["complete"], "bytes=", human(entry["bytes"]),
              "files=", entry["files"], "dirs=", entry["dirs"],
              "reason=", entry["reason"] or "-")
    print("CATEGORIES BY ACTUAL SIZE:")
    for row in categories[:55]:
        print(" ", row["name"], "size=", human(row["bytes"]),
              "files=", row["files"], "dirs=", row["dirs"],
              "older7d=", human(row["older_7d_bytes"]))
    print("APK FILE TYPES BY SIZE:")
    for row in result["apk_extensions"][:20]:
        print(" ", row["extension"], human(row["bytes"]), "files=", row["files"])
    print("TOP DATA DIRECTORIES:")
    for row in top_directories[:30]:
        print(" ", row["name"], human(row["bytes"]), "files=", row["files"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
