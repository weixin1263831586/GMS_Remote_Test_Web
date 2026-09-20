"""Layout gate for the tools/ directory.

tools/ 是外部工具与项目维护脚本的存放地（见 tools/README.md）：

* 一级目录只允许第三方/独立维护工具；
* 项目自维护的 .py/.sh 必须收进 tools/scripts/<category>/；
* tools/scripts 下的 .py/.sh 只能按白名单分类目录组织。

门禁目的：Agent/开发者新增维护脚本时不再往 tools/ 根目录扔散文件。
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"

ALLOWED_TOP_LEVEL_DIRS = {
    "GMS-Host-Tools",
    "adbproxy-rs",
    "android-internals-wiki",
    "gms-worker-native",
    "jadx",
    "scripts",
    "tesseract",
    "upgrade_tool",
    "usbip",
}

ALLOWED_SCRIPT_CATEGORIES = {
    "_common.py",
    "agent",
    "deployment",
    "docs",
    "maintenance",
    "migrations",
    "setup",
    "testing",
    "utilities",
}

PROJECT_SCRIPT_SUFFIXES = {".py", ".sh"}


def test_tools_root_contains_no_project_scripts():
    """tools/ 根目录不得直接放置项目维护脚本（.py/.sh）。"""
    forbidden = [
        entry
        for entry in TOOLS.iterdir()
        if entry.is_file() and entry.suffix in PROJECT_SCRIPT_SUFFIXES
    ]
    assert forbidden == [], (
        "project-maintained scripts must live under tools/scripts/<category>/, "
        f"found at tools/ root: {sorted(p.name for p in forbidden)}"
    )


def test_tools_root_directories_are_allowlisted():
    """一级目录须在白名单内，新增第三方工具目录时同步更新本测试与 README。"""
    unexpected = [
        entry.name
        for entry in TOOLS.iterdir()
        if entry.is_dir() and entry.name not in ALLOWED_TOP_LEVEL_DIRS
    ]
    assert unexpected == [], (
        "unexpected top-level directory under tools/ "
        f"{sorted(unexpected)}; update tools/README.md and "
        "ALLOWED_TOP_LEVEL_DIRS deliberately"
    )


def test_tools_scripts_only_contains_known_categories():
    """tools/scripts/ 只允许白名单分类目录与共享 _common.py（忽略缓存目录）。"""
    unexpected = [
        entry.name
        for entry in (TOOLS / "scripts").iterdir()
        if entry.name not in ALLOWED_SCRIPT_CATEGORIES
        and not (entry.is_dir() and entry.name == "__pycache__")
    ]
    assert unexpected == [], (
        f"unexpected entries under tools/scripts/: {sorted(unexpected)}; "
        "add a categorized subdirectory, not loose files"
    )


def test_scripts_import_shared_repo_root_helper():
    """分类脚本定位 repo root 必须走 _common.find_repo_root，禁止 parents[N] 硬编码。"""
    offenders: list[str] = []
    for script in (TOOLS / "scripts").rglob("*.py"):
        text = script.read_text(encoding="utf-8")
        if "Path(__file__)" not in text:
            continue
        if "_common" in text:
            continue
        if ".parents[" in text or "parent.parent" in text:
            offenders.append(str(script.relative_to(ROOT)))
    assert offenders == [], (
        "resolve the repo root via tools/scripts/_common.find_repo_root "
        f"instead of hardcoded parents hops: {offenders}"
    )


def test_third_party_tool_dirs_have_provenance_manifest():
    """usbip 模式：每个第三方工具目录必须自带 <name>.provenance.json 溯源清单。

    清单集中记录 fork/upstream URL、锁定版本与更新方式；上游同名清单
    （如 usbipd.provenance.json 之于 usbip）按工件命名。
    """
    import json

    for directory, manifest_name in (
        ("usbip", "usbipd.provenance.json"),
        ("adbproxy-rs", "adbproxy-rs.provenance.json"),
        ("jadx", "jadx.provenance.json"),
    ):
        manifest = TOOLS / directory / manifest_name
        assert manifest.is_file(), f"缺少溯源清单: {manifest}"
        entry = json.loads(manifest.read_text(encoding="utf-8"))
        for field in ("source", "upstream", "update_procedure"):
            assert entry.get(field), (
                f"{manifest_name}: missing required field {field!r}"
            )
        assert entry["source"].startswith("https://"), (
            f"{manifest_name}: source must be a URL"
        )


def test_third_party_tool_dirs_track_artifacts_only():
    """usbip 模式：第三方工具目录提交到 git 的只允许白名单工件。

    源码/上游文档属于本地 clone 或上游仓库，被 .gitignore 排除；意外
    入库时本测试立即失败。
    """
    import fnmatch
    import subprocess

    allowed_patterns = {
        "tools/usbip/": ("usbipd", "usbipd.provenance.json"),
        "tools/adbproxy-rs/": (
            "adbproxy-rs.provenance.json",
            "dist/*",
        ),
        "tools/jadx/": (
            "jadx.provenance.json",
            "bin/*",
            "lib/*",
        ),
    }
    for prefix, patterns in allowed_patterns.items():
        tracked = subprocess.run(
            ["git", "ls-files", prefix],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
        unexpected = sorted(
            path
            for path in tracked
            if not any(
                fnmatch.fnmatch(path, f"{prefix}{pattern}")
                for pattern in patterns
            )
        )
        assert unexpected == [], (
            f"usbip 模式下 {prefix} 只允许提交 {patterns}，"
            f"意外入库: {unexpected}"
        )
