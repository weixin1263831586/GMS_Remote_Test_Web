"""tools/usbip/usbipd 与其目录级 provenance 清单的 sha256 一致性校验。

随平台分发的 ``tools/usbip/usbipd`` 是一个 ELF 二进制，Git 历史无法证明
它来自 usbip 仓库哪个 commit。``tools/usbip/usbipd.provenance.json``
记录 source commit、版本、sha256 等溯源信息（usbip 模式：目录只保留
工件与自己的 provenance 清单，源码不入主仓库）；本测试保证：

1. 清单存在且字段齐全；
2. 二进制真实 sha256 与清单一致——替换二进制时必须同步更新清单。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "tools" / "usbip" / "usbipd.provenance.json"
BINARY = REPO_ROOT / "tools" / "usbip" / "usbipd"

REQUIRED_FIELDS = (
    "source",
    "upstream",
    "release_tag",
    "commit",
    "target",
    "sha256",
    # v0.9.6 起二进制来自 usbip 仓库 GitHub Release 产物，不再本地
    # cargo build，溯源锚点是 release_url（下载来源）而非 Cargo.lock。
    "release_url",
    "rustc",
)


def test_provenance_manifest_exists_and_complete():
    assert MANIFEST.is_file(), "tools/usbip/usbipd.provenance.json 缺失"
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    missing = [field for field in REQUIRED_FIELDS if not data.get(field)]
    assert not missing, f"provenance 清单缺少字段: {missing}"


def test_usbipd_binary_matches_indexed_sha256():
    assert BINARY.is_file(), "tools/usbip/usbipd 缺失"
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual = hashlib.sha256(BINARY.read_bytes()).hexdigest()
    assert actual == data["sha256"], (
        "tools/usbip/usbipd 的 sha256 与 provenance 清单不一致："
        f"实际 {actual}，清单 {data['sha256']}。替换二进制后必须同步更新清单。"
    )
