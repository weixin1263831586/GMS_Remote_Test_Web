"""Unified artifact provenance gate for third-party tools under tools/.

历史教训（评审 2026-09）：usbip 有 ``tests/test_usbipd_provenance.py`` 的
SHA256 强校验，而 adbproxy-rs/jadx/scrcpy/upgrade_tool/misc.img 各自成
色不一——jadx 的 provenance 甚至登记了错误的 license 且不携带任何
哈希。散落式"每工具一个专用测试"会越来越散，本测试改为统一 schema：

* 目录级清单（``<name>.provenance.json``，usbip 模式）与
  ``tools/utilities.provenance.json``（根目录散装分发工件）共用
  ``artifact_sha256`` 字段：key 为仓库相对路径，value 为十六进制
  SHA256，替换任何工件必须同步刷新清单；
* jadx（bin/ + lib/ 随平台直接分发）必须提供覆盖全部已跟踪工件的
  ``artifact_sha256``，且 license 登记为上游实际表达式（Apache-2.0，
  LICENSE/NOTICE 随目录分发）；其余目录清单若声明了 ``artifact_sha256``
  则逐条强校验（usbip 主工件另有 ``sha256`` 单字段 + 既有专项测试）。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
UTILITIES_MANIFEST = TOOLS / "utilities.provenance.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_DIRECTORY_MANIFESTS = {
    "tools/usbip/usbipd.provenance.json",
    "tools/adbproxy-rs/adbproxy-rs.provenance.json",
    "tools/jadx/jadx.provenance.json",
}

#: 这些目录把 bin/ + lib/（等）工件直接提交进主仓库并随平台分发，
#: 必须登记覆盖全部已跟踪工件的 artifact_sha256（不能只留一个上游 URL）。
_FULL_COVERAGE_REQUIRED = {
    "tools/jadx/jadx.provenance.json",
}

_JADX_LICENSE_PREFIX = "Apache-2.0"


def _tracked_files_under(prefix: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", prefix],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [p for p in out if not p.endswith(".provenance.json")]


def _present_files_under(directory: Path) -> set[str]:
    """目录内实际存在的分发文件（相对仓库根；排除 provenance 清单自身）。

    用实际文件而不是 git ls-files：新替换/恢复的工件在未暂存时也要
    立即纳入登记，不能等 commit 后才被门禁覆盖。
    """
    return {
        p.relative_to(ROOT).as_posix()
        for p in directory.rglob("*")
        if p.is_file() and p.suffix != ".json"
    }


def _actual_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_entry_shape(where: str, entry: dict) -> None:
    for field in ("source", "pinned_version", "license", "sha256", "update_procedure"):
        assert str(entry.get(field) or "").strip(), f"{where}: missing field {field!r}"
    assert str(entry["source"]).startswith("https://"), (
        f"{where}: source must be an https URL"
    )
    assert _SHA256_RE.match(str(entry["sha256"])), (
        f"{where}: sha256 must be a 64-char lowercase hex digest"
    )


def test_directory_manifests_share_unified_artifact_sha256():
    """目录级 provenance 清单统一 schema：声明即强校验，jadx 全覆盖。"""
    for manifest_rel in sorted(_DIRECTORY_MANIFESTS):
        manifest = ROOT / manifest_rel
        assert manifest.is_file(), f"缺少溯源清单: {manifest}"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        entries = data.get("artifact_sha256")
        if entries is None:
            # legacy 单工件清单（usbip/adbproxy 已由既有专项测试覆盖主工件）
            assert manifest_rel not in _FULL_COVERAGE_REQUIRED, (
                f"{manifest_rel}: 随平台直接分发工件的清单必须提供 artifact_sha256"
            )
            continue
        assert isinstance(entries, dict) and entries, (
            f"{manifest_rel}: artifact_sha256 必须是非空映射"
        )
        prefix = manifest_rel.rsplit("/", 1)[0] + "/"
        tracked = set(_tracked_files_under(prefix)) | _present_files_under(ROOT / prefix)
        for rel, digest in entries.items():
            path = ROOT / rel
            assert rel in tracked, f"{manifest_rel}: artifact_sha256 引用了未跟踪文件 {rel}"
            assert path.is_file(), f"{manifest_rel}: 工件缺失 {rel}"
            assert _SHA256_RE.match(str(digest)), (
                f"{manifest_rel}: {rel} 的 sha256 不是合法摘要"
            )
            actual = _actual_sha256(path)
            assert actual == digest, (
                f"{manifest_rel}: {rel} sha256 不一致（实际 {actual}，清单 {digest}）。"
                "替换工件后必须同步更新清单。"
            )
        if manifest_rel in _FULL_COVERAGE_REQUIRED:
            missing = sorted(tracked - set(entries))
            assert not missing, (
                f"{manifest_rel}: 以下已跟踪分发工件未登记 artifact_sha256: {missing}"
            )


def test_jadx_license_matches_upstream_notices():
    """jadx 元数据错误回归：上游 v1.4.0 是 Apache-2.0（非 GPL），LICENSE/NOTICE 随目录分发。"""
    manifest = TOOLS / "jadx" / "jadx.provenance.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert str(data.get("license", "")).startswith(_JADX_LICENSE_PREFIX), (
        "jadx.provenance.json: license 必须登记为上游实际的 Apache-2.0"
    )
    for notice in ("LICENSE", "NOTICE"):
        path = TOOLS / "jadx" / notice
        assert path.is_file(), f"tools/jadx/{notice} 必须随目录分发（上游原文）"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "Apache License" in text, (
            f"tools/jadx/{notice} 内容与 Apache-2.0 声明不符，请核对下载来源"
        )


def test_utilities_manifest_covers_root_level_artifacts():
    """tools/ 根目录散装分发工件必须有统一溯源清单且哈希一致。"""
    assert UTILITIES_MANIFEST.is_file(), f"缺少统一清单: {UTILITIES_MANIFEST}"
    data = json.loads(UTILITIES_MANIFEST.read_text(encoding="utf-8"))
    utilities = data.get("utilities")
    assert isinstance(utilities, dict) and utilities, (
        "utilities.provenance.json: utilities 必须是非空映射"
    )
    for rel, entry in sorted(utilities.items()):
        where = f"utilities[{rel}]"
        _assert_entry_shape(where, entry)
        path = ROOT / rel
        assert path.is_file(), f"{where}: 工件缺失 {rel}"
        actual = _actual_sha256(path)
        assert actual == entry["sha256"], (
            f"{where}: sha256 不一致（实际 {actual}，清单 {entry['sha256']}）。"
            "替换工件后必须同步更新清单。"
        )
