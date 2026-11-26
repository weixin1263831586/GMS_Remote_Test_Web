"""Read-only, bounded runtime artifact inventory for the Security Audit page.

Count every regular file under the Controller data root exactly once. A
single traversal replaces the old shared 120k-entry budget that consistently
stopped inside JADX output and hid the 3 GiB Redmine evidence directory.

This module never reads file bodies, resolves/follows symlinks, or mutates the
filesystem. Ages indicate only *mtime*, not eligibility for deletion.
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundation.config import PROJECT_ROOT, settings
from foundation.ops_port import ArtifactBreakdown, ArtifactCounters, ArtifactItem, DiskInventoryResult, FilesystemUsage
from foundation.uploads import upload_temp_root


MAX_VISITED_ENTRIES = 1_200_000
MAX_SCAN_SECONDS = 45.0
MAX_OWNER_ENTRIES = 1000
MAX_TEMP_DISCOVERY_ENTRIES = 20000
MAX_TREE_DEPTH = 96
CACHE_SECONDS = 60.0
_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, str, DiskInventoryResult] | None = None

# Stable category IDs are also used by UI filters and regression tests.
# id, label, group, cleanup policy, attention, reference mtime age, explanation
ARTIFACT_KINDS = (
    ("firmware_chunks", "固件分片暂存", "固件", "条件清理", "高", 1,
     "成功烧写后清理，失败分片依赖后续上传触发过期回收"),
    ("firmware_stage", "ATS 固件共享暂存", "固件", "人工确认", "高", 7,
     "完整镜像需要引用校验；常规后台只清理过期 .part"),
    ("cluster_firmware", "集群固件烧写暂存", "固件", "任务结束", "高", 1,
     "依赖 Worker 终态 ACK；失败或失联可能留下镜像"),
    ("apk", "APK 原包与反编译", "APK", "任务配额", "高", 30,
     "包含 APK 原包、JADX Java/Class 代码及资源；按任务数淘汰而非年龄"),
    ("cluster_artifacts", "集群测试产物", "测试", "策略可选", "高", 30,
     "日志归档策略默认关闭，测试报告归档有独立生命周期"),
    ("cluster_uploads", "集群产物上传分片", "测试", "条件清理", "高", 1,
     "中断或无终态回执的上传分片可能残留"),
    ("cluster_transfers", "集群跨主机传输", "测试", "条件清理", "高", 7,
     "部分传输消费后回收，未消费和中断状态需核对"),
    ("cluster_other", "集群数据库与其他文件", "测试", "历史保留", "中", 90,
     "包含集群业务数据库及未归入单独产物目录的文件"),
    ("local_worker_jobs", "本机 Worker Job 日志", "测试", "未配置", "高", 7,
     "仅本机默认 Worker jobs 路径，不代表其他主机 Worker 数据"),
    ("report_index", "报告管理索引", "报告", "历史保留", "低", 90,
     "仅 Controller 报告索引，不包含 Worker 套件原始 results/logs"),
    ("test_logs", "人工保存日志与 ZIP", "报告", "未配置", "高", 30,
     "应用侧未发现周期清理；扫描 GMS_TEST_LOG_ROOT 或默认位置"),
    ("report_temp", "报告分析临时文件", "报告", "正常结束删除", "中", 1,
     "仅扫描 /tmp 中已知 GMS 报告临时前缀，异常中断可能残留"),
    ("redmine_evidence", "Redmine 原始证据", "Agent", "快照策略", "高", 90,
     "原始附件和证据快照；中断态可能绕过保留规则"),
    ("redmine_attachments", "Redmine 历史附件", "Agent", "未配置", "高", 90,
     "历史分析附件目录未发现周期清理"),
    ("redmine_docs", "Redmine Markdown 报告", "Agent", "未配置", "中", 90,
     "每次分析可产生独立运行报告"),
    ("redmine_databases", "Redmine 分析数据库", "Agent", "历史保留", "中", 90,
     "包含 SQLite、WAL/SHM；文件年龄绝不代表可删库"),
    ("redmine_other", "Redmine 其他文件", "Agent", "历史保留", "中", 90,
     "未匹配既有证据/附件/报告类型的 Redmine 数据"),
    ("agent_transcripts", "对话 Agent 会话记录", "Agent", "手动删除", "中", 90,
     "对话记录与恢复锚点分开存储；不按文件年龄自动删除"),
    ("security_audit_logs", "安全审计日志与轮换", "日志", "日志轮换", "低", 30,
     "Controller 审计日志及轮换文件，需保留审计完整性"),
    ("serial_logs", "设备串口日志", "日志", "条件自动", "低", 14,
     "活跃串口写入触发保留期清理；静默设备可能遗留"),
    ("config_cache", "配置浏览缓存", "其他", "缓存管理", "中", 7,
     "包括 config_explorer_cache 的缓存文件"),
    ("generic_uploads", "通用上传临时文件", "其他", "条件清理", "中", 7,
     "后台任务开启时定期清理过期上传会话"),
    ("runtime_other", "Controller 其他数据", "其他", "按业务保留", "低", 90,
     "包含其余数据库、配置、索引与未单列数据；不是待清理清单"),
)

# Names are intentionally static: never send user/issue/job identifiers or
# arbitrary server file paths to the browser.
DISPLAY_PATHS = {
    "firmware_chunks": "uploads/gms_firmware_uploads",
    "firmware_stage": "data/automation/artifact-stage",
    "cluster_firmware": "data/cluster/firmware",
    "apk": "data/apk_uploads",
    "cluster_artifacts": "data/cluster/artifacts",
    "cluster_uploads": "data/cluster/artifact-uploads",
    "cluster_transfers": "data/cluster/transfers",
    "cluster_other": "data/cluster（其他文件）",
    "local_worker_jobs": "~/gms-worker-data/jobs（本机）",
    "report_index": "data/reports",
    "test_logs": "GMS_TEST_LOG_ROOT（默认 /tmp/test-logs）",
    "report_temp": "/tmp/gms-report-download-* 等",
    "redmine_evidence": "data/redmine/by_user/*/evidence",
    "redmine_attachments": "data/redmine/{attachments,by_user/*/attachments}",
    "redmine_docs": "data/redmine/{docs,by_user/*/docs}",
    "redmine_databases": "data/redmine/{*.sqlite3,by_user/*/*.sqlite3} + WAL/SHM",
    "redmine_other": "data/redmine（其他文件）",
    "agent_transcripts": "project/data/assistant/agent_transcripts",
    "security_audit_logs": "data/security_audit*",
    "serial_logs": "data/devices_console/logs",
    "config_cache": "data/config_explorer_cache",
    "generic_uploads": "uploads/gms_uploads（及其他上传会话）",
    "runtime_other": "data/其他业务数据及配置",
}
_OWNER_CATEGORIES = {
    "redmine_evidence", "redmine_attachments", "redmine_docs",
    "redmine_databases", "redmine_other",
}
_SQLITE_SUFFIXES = (".sqlite3", ".sqlite3-wal", ".sqlite3-shm")
_REPORT_TEMP_PREFIXES = ("gms-report-download-", "report_agent_", "redmine_agent_report_")


def _kind(path: tuple[str, ...]) -> str | None:
    if not path:
        return None
    first = path[0]
    if first == "apk_uploads":
        return "apk"
    if first == "uploads":
        if len(path) >= 2 and path[1] == "gms_firmware_uploads":
            return "firmware_chunks"
        return "generic_uploads"
    if first == "automation" and len(path) >= 2 and path[1] == "artifact-stage":
        return "firmware_stage"
    if first == "cluster":
        sub = path[1] if len(path) > 1 else ""
        return {
            "firmware": "cluster_firmware",
            "artifacts": "cluster_artifacts",
            "artifact-uploads": "cluster_uploads",
            "transfers": "cluster_transfers",
        }.get(sub, "cluster_other")
    if first == "redmine":
        rest = path[1:]
        if len(rest) >= 2 and rest[0] == "by_user":
            rest = rest[2:]  # discard the owner's identity
        if rest and rest[0] == "evidence":
            return "redmine_evidence"
        if rest and rest[0] == "attachments":
            return "redmine_attachments"
        if rest and rest[0] == "docs":
            return "redmine_docs"
        if rest and (rest[0].endswith(_SQLITE_SUFFIXES)
                     or rest[0].endswith(".sqlite3-journal")):
            return "redmine_databases"
        return "redmine_other"
    if first == "reports":
        return "report_index"
    if first == "devices_console" and len(path) > 1 and path[1] == "logs":
        return "serial_logs"
    if first == "assistant" and len(path) > 1 and path[1] == "agent_transcripts":
        return "agent_transcripts"
    if first.startswith("security_audit"):
        return "security_audit_logs"
    if first == "config_explorer_cache":
        return "config_cache"
    return "runtime_other"


def _new_row() -> ArtifactCounters:
    return {
        "bytes": 0, "files": 0, "directories": 0, "aged_bytes": 0,
        "aged_files": 0, "older_7d_bytes": 0, "older_30d_bytes": 0,
        "older_90d_bytes": 0, "newest": None, "present": False,
        "complete": True, "note": "",
    }


def _unsafe_path_component(path: Path) -> str:
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return "扫描根的路径组件不可访问"
        if stat.S_ISLNK(info.st_mode):
            return "路径包含符号链接，未扫描链接目标"
    return ""


def _record_error(row: ArtifactCounters, detail: str) -> None:
    row["complete"] = False
    if detail not in row["note"].split("；"):
        row["note"] = "；".join(filter(None, (row["note"], detail)))


def _scan_tree(
    root: Path, classifier: Callable[[tuple[str, ...]], str | None],
    rows: dict[str, ArtifactCounters], *,
    now: float, deadline: float, limit: int, apk_types: dict[str, dict[str, int]],
    owner_limit: int = MAX_OWNER_ENTRIES,
    excluded_roots: frozenset[str] = frozenset(),
) -> tuple[int, bool]:
    """Iterative depth-first scandir. Never materializes huge JADX file lists."""
    unsafe = _unsafe_path_component(root)
    if unsafe:
        for row in rows.values():
            _record_error(row, unsafe)
        return 0, False
    try:
        initial = root.lstat()
    except FileNotFoundError:
        return 0, True
    except OSError:
        for row in rows.values():
            _record_error(row, "扫描根无法读取")
        return 0, False

    def account(parts: tuple[str, ...], info: os.stat_result) -> None:
        key = classifier(parts)
        if key is None:
            return
        row = rows[key]
        row["present"] = True
        if stat.S_ISDIR(info.st_mode):
            row["directories"] += 1
            return
        if not stat.S_ISREG(info.st_mode):
            return
        size = info.st_size
        row["files"] += 1
        row["bytes"] += size
        row["newest"] = max(row["newest"] or info.st_mtime, info.st_mtime)
        days = (now - info.st_mtime) / 86400
        if days > 7:
            row["older_7d_bytes"] += size
        if days > 30:
            row["older_30d_bytes"] += size
        if days > 90:
            row["older_90d_bytes"] += size
        # Reference age is a per-category *indicator*, never cleanup eligibility.
        reference = _REF_DAYS[key]
        if days > reference:
            row["aged_bytes"] += size
            row["aged_files"] += 1
        if key == "apk":
            extension = os.path.splitext(parts[-1])[1].lower()[:20] or "(无后缀)"
            stat_row = apk_types[extension]
            stat_row["bytes"] += size
            stat_row["files"] += 1

    visited = 0
    if stat.S_ISREG(initial.st_mode):
        account((root.name,), initial)
        return 1, True
    if not stat.S_ISDIR(initial.st_mode):
        return 0, True

    try:
        handle = os.scandir(root)
    except OSError:
        for row in rows.values():
            _record_error(row, "目录不可遍历")
        return 0, False
    # Each stack frame owns exactly one scandir iterator; close on all exits.
    stack: list[tuple[Any, tuple[str, ...], int]] = [(handle, (), 0)]
    complete = True
    truncated = False
    try:
        while stack:
            if visited >= limit or time.monotonic() >= deadline:
                complete = False
                truncated = True
                break
            iterator, prefix, count = stack[-1]
            try:
                entry = next(iterator)
                stack[-1] = (iterator, prefix, count + 1)
            except StopIteration:
                iterator.close()
                stack.pop()
                continue
            except OSError:
                complete = False
                key = classifier(prefix)
                if key:
                    _record_error(rows[key], "目录部分条目读取失败")
                iterator.close()
                stack.pop()
                continue
            parts = (*prefix, entry.name)
            if excluded_roots and os.path.abspath(entry.path) in excluded_roots:
                # Nested roots are scanned under their own artifact category.
                continue
            key = classifier(parts)
            # 1000+ owners are usually a configuration error; never silently skip.
            if prefix == ("redmine", "by_user") and count >= owner_limit:
                complete = False
                for category in _OWNER_CATEGORIES:
                    _record_error(rows[category], "用户目录枚举达到数量保护上限")
                continue
            visited += 1
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                complete = False
                if key:
                    _record_error(rows[key], "文件元数据不可读取")
                continue
            if stat.S_ISLNK(info.st_mode):
                complete = False
                targets = _OWNER_CATEGORIES if parts == ("redmine", "by_user") else (
                    {"firmware_stage"} if parts == ("automation",) else
                    {key} if key else set(rows)
                )
                for category in targets:
                    if category in rows:
                        _record_error(rows[category], "存在跳过的符号链接")
                continue
            account(parts, info)
            if not stat.S_ISDIR(info.st_mode):
                continue
            if len(stack) >= MAX_TREE_DEPTH:
                complete = False
                if key:
                    _record_error(rows[key], "目录层级达到保护上限")
                continue
            try:
                nested = os.scandir(entry.path)
            except OSError:
                complete = False
                if key:
                    _record_error(rows[key], "目录不可读取")
                continue
            stack.append((nested, parts, 0))
    finally:
        for handle, _, _ in stack:
            handle.close()

    if truncated:
        # An unknown portion of the tree was not inspected; don't represent
        # any category in that tree as having an exact total.
        for row in rows.values():
            if row["complete"]:
                _record_error(row, "扫描未覆盖全部目录，当前大小为下限")
    return visited, complete


_REF_DAYS = {kind[0]: kind[5] for kind in ARTIFACT_KINDS}


def _additional_roots(data_root: Path, *, test_log_root: Path | None,
                      worker_root: Path | None, requested_data_root: bool,
                      deadline: float | None = None) -> tuple[list[tuple[Path, str]], str]:
    logs = Path(test_log_root or os.getenv("GMS_TEST_LOG_ROOT", "/tmp/test-logs"))
    worker = (
        Path(worker_root) if worker_root is not None else
        (data_root / "__test_no_worker__" if requested_data_root else
         Path.home() / "gms-worker-data" / "jobs")
    )
    transcripts = (
        data_root / "assistant" / "agent_transcripts" if requested_data_root
        else Path(PROJECT_ROOT) / "data" / "assistant" / "agent_transcripts"
    )
    result = [
        (Path(upload_temp_root("gms_firmware_uploads")), "firmware_chunks"),
        (Path(upload_temp_root()), "generic_uploads"),
        (logs, "test_logs"),
        (worker, "local_worker_jobs"),
        (transcripts, "agent_transcripts"),
    ]
    if requested_data_root or test_log_root is not None:
        return result, ""
    note = ""
    deadline = deadline if deadline is not None else time.monotonic() + MAX_SCAN_SECONDS
    candidates: list[Path] = []
    try:
        with os.scandir("/tmp") as entries:
            for visited, entry in enumerate(entries):
                if visited >= MAX_TEMP_DISCOVERY_ENTRIES:
                    note = f"临时目录枚举达到 {MAX_TEMP_DISCOVERY_ENTRIES} 项保护上限"
                    break
                if time.monotonic() >= deadline:
                    note = "临时报告目录枚举达到时间保护上限"
                    break
                if entry.name.startswith(_REPORT_TEMP_PREFIXES):
                    if len(candidates) >= 200:
                        note = "临时报告目录超出 200 个扫描上限"
                        break
                    candidates.append(Path(entry.path))
        result.extend((candidate, "report_temp") for candidate in candidates)
    except OSError:
        note = "临时报告目录无法完整枚举"
    return result, note


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path.absolute().is_relative_to(parent.absolute())
    except ValueError:
        return False


def collect_disk_artifacts(
    data_root: Path | None = None, *,
    test_log_root: Path | None = None,
    worker_root: Path | None = None,
) -> DiskInventoryResult:
    """Read known host-local paths once; never contact or alter remote hosts."""
    start = time.monotonic()
    now = time.time()
    data = Path(data_root if data_root is not None else settings.data_root).absolute()
    rows = {row[0]: _new_row() for row in ARTIFACT_KINDS}
    apk_types: dict[str, dict[str, int]] = defaultdict(lambda: {"bytes": 0, "files": 0})
    # The current Agent writes transcripts under PROJECT_ROOT/data, even if
    # GMS_DATA_ROOT points elsewhere. Old copies in the custom data root are
    # counted as miscellaneous Controller data, not double attributed to Agent.
    actual_transcripts = Path(PROJECT_ROOT).absolute() / "data" / "assistant" / "agent_transcripts"
    standard_transcripts = data / "assistant" / "agent_transcripts"

    def controller_kind(parts: tuple[str, ...]) -> str | None:
        if (data_root is None and actual_transcripts != standard_transcripts
                and parts[:2] == ("assistant", "agent_transcripts")):
            return "runtime_other"
        return _kind(parts)

    total_visits = 0
    entries, controller_complete = _scan_tree(
        data, controller_kind, rows, now=now,
        deadline=start + MAX_SCAN_SECONDS,
        limit=MAX_VISITED_ENTRIES, apk_types=apk_types,
        owner_limit=MAX_OWNER_ENTRIES,
    )
    total_visits += entries
    controller_bytes = sum(row["bytes"] for row in rows.values())
    # These roots are outside data/ on some deployments. The containment check
    # prevents counting upload/transcript/log files twice.
    external_roots, temp_note = _additional_roots(
        data, test_log_root=test_log_root, worker_root=worker_root,
        requested_data_root=data_root is not None,
        deadline=start + MAX_SCAN_SECONDS,
    )
    if temp_note:
        _record_error(rows["report_temp"], temp_note)
    # Build the disjoint scan plan before traversing any external root.
    seen = {str(data)}
    scan_roots: list[tuple[Path, str]] = []
    for root, key in external_roots:
        identity = str(root.absolute())
        if _inside(root, data) or identity in seen:
            continue
        seen.add(identity)
        scan_roots.append((root, key))
    local_worker_scanned = False
    for root, key in scan_roots:
        excluded = frozenset(
            str(child.absolute()) for child, _ in scan_roots
            if child != root and _inside(child, root)
        )
        if _inside(data, root):
            excluded = excluded | frozenset((str(data),))
        per_root = {key: rows[key]}
        if key == "local_worker_jobs" and root.exists() and not root.is_symlink():
            local_worker_scanned = True
        count, _ = _scan_tree(
            root, lambda _parts, selected=key: selected,
            per_root, now=now, deadline=start + MAX_SCAN_SECONDS,
            limit=max(0, MAX_VISITED_ENTRIES - total_visits),
            apk_types=apk_types, excluded_roots=excluded,
        )
        total_visits += count
    measured = sum(row["bytes"] for row in rows.values())
    incomplete = sum(not row["complete"] for row in rows.values())
    filesystem: FilesystemUsage
    try:
        disk = shutil.disk_usage(data)
        filesystem = {"total_bytes": disk.total, "free_bytes": disk.free, "used_bytes": disk.used}
    except OSError:
        filesystem = {"total_bytes": None, "free_bytes": None, "used_bytes": None}
    items: list[ArtifactItem] = []
    for key, label, group, cleanup, priority, days, explanation in ARTIFACT_KINDS:
        row = rows[key]
        item: ArtifactItem = {
            **row,
            "id": key, "name": label, "group": group,
            "scope": "本机 Worker" if key == "local_worker_jobs" else "Controller",
            "location": DISPLAY_PATHS[key], "cleanup": cleanup,
            "priority": priority, "age_days": days, "description": explanation,
            "status": (
                "partial" if not row["complete"] else
                "unavailable" if key == "local_worker_jobs" and not local_worker_scanned else
                "scanned" if row["present"] else "absent"
            ),
        }
        if key == "apk":
            breakdown: list[ArtifactBreakdown] = [
                {"type": ext, "bytes": value["bytes"], "files": value["files"]}
                for ext, value in apk_types.items()
            ]
            item["breakdown"] = sorted(
                breakdown, key=lambda value: value["bytes"], reverse=True,
            )[:15]
        items.append(item)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "Controller 数据根及同主机可访问的固定目录",
        "readonly": True,
        "cache_hit": False,
        "summary": {
            "measured_bytes": measured,
            "controller_data_bytes": controller_bytes,
            "local_worker_bytes": rows["local_worker_jobs"]["bytes"],
            "aged_bytes": sum(row["aged_bytes"] for row in rows.values()),
            "scanned_categories": len(items) - incomplete,
            "incomplete_categories": incomplete,
            "attention_categories": sum(
                row["priority"] == "高" and row["bytes"] > 0 for row in items
            ),
            "visited_entries": total_visits,
            "scan_seconds": round(time.monotonic() - start, 2),
            "controller_scan_complete": controller_complete,
            "disk": filesystem,
        },
        "items": items,
        "coverage": [
            "完整遍历 Controller data 根并按真实路径分类，未分类数据也计入汇总",
            "本机 Worker 默认 jobs 目录单独扫描；远端 Worker 与 Windows Source 尚未接入",
            "远端 CTS/GTS/VTS/STS 套件 results/logs、SDK 和固件库未纳入",
            "整个系统盘已用空间不能直接等同于本平台运行产物大小",
            "参考超龄仅依据文件修改时间；不构成删除或释放空间的依据",
        ],
    }


def cached_disk_artifacts(refresh: bool = False) -> DiskInventoryResult:
    """A manual refresh always resamples the disk, even inside the cache TTL."""
    global _CACHE
    key = str(Path(settings.data_root).absolute())
    with _CACHE_LOCK:
        if not refresh and _CACHE and _CACHE[1] == key and (
            time.monotonic() - _CACHE[0] < CACHE_SECONDS
        ):
            return {**_CACHE[2], "cache_hit": True}
        result = collect_disk_artifacts()
        _CACHE = (time.monotonic(), key, result)
        return result
