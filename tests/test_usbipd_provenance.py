"""tools/usbipd provenance manifest 校验。

随平台分发的 ``tools/usbipd`` 是一个 ELF 二进制，Git 历史无法证明它来自
usbip 仓库哪个 commit（2026-09-03 联合审计 P1-5）。``tools/usbipd.provenance.json``
记录 source commit、版本、sha256 等溯源信息；本测试保证：

1. 清单存在且字段齐全；
2. 二进制真实 sha256 与清单一致——替换二进制时必须同步更新清单。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "tools" / "usbipd.provenance.json"
BINARY = REPO_ROOT / "tools" / "usbipd"

REQUIRED_FIELDS = (
    "source_repo",
    "commit",
    "version",
    "target",
    "sha256",
    "cargo_lock_sha256",
    "rustc",
)


def test_provenance_manifest_exists_and_complete():
    assert MANIFEST.is_file(), "tools/usbipd.provenance.json 缺失"
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    missing = [field for field in REQUIRED_FIELDS if not data.get(field)]
    assert not missing, f"provenance 清单缺少字段: {missing}"


def test_bundled_usbipd_sha256_matches_manifest():
    assert BINARY.is_file(), "tools/usbipd 缺失"
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    digest = hashlib.sha256(BINARY.read_bytes()).hexdigest()
    assert digest == data["sha256"], (
        "tools/usbipd 的 sha256 与 provenance 清单不一致："
        f"binary={digest} manifest={data['sha256']}。"
        "请先在 usbip 仓库重建并记录新哈希，再同步更新清单。"
    )
