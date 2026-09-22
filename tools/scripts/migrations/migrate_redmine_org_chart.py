#!/usr/bin/env python3
"""一次性迁移：旧扁平 configs/redmine_user_map.json → configs/local/redmine_org_chart.json。

方案 2 落地：组织架构改为全局共享（configs/local），per-owner 文件降级为
个人 overlay（自我绑定 + 别名）。此脚本只搬组织架构；各 owner 的旧
per-owner 文件保留原样（其中的 departments 键被新读侧忽略，个人别名
如需保留可后续手工并入）。

用法：python tools/scripts/migrations/migrate_redmine_org_chart.py [--dry-run]
幂等：canonical 已存在时不覆盖（除非 --force）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root  # noqa: E402


PROJECT_ROOT = find_repo_root()
sys.path.insert(0, str(PROJECT_ROOT))

from features.redmine.org_chart import (  # noqa: E402
    ORG_CHART_FILENAME,
    load_org_payload,
    save_org_payload,
)
from foundation.config_paths import config_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 canonical 文件")
    parser.add_argument("--source", type=Path, default=None,
                        help="旧扁平 user_map 路径（默认 configs/redmine_user_map.json，"
                             "可指向旧部署文件）")
    args = parser.parse_args()

    root = config_root(PROJECT_ROOT)
    legacy = args.source or (root / "redmine_user_map.json")
    canonical = root / "local" / ORG_CHART_FILENAME

    if canonical.exists() and not args.force:
        print(f"SKIP: {canonical} 已存在（--force 覆盖）")
        return 0
    if not legacy.exists():
        print(f"无旧文件可迁移：{legacy} 不存在")
        return 1
    try:
        payload = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"旧文件解析失败: {exc}")
        return 1
    departments = payload.get("departments") if isinstance(payload, dict) else None
    if not isinstance(departments, list):
        print("旧文件缺少 departments 列表，无法迁移")
        return 1
    members = sum(
        len(dept.get("members") or [])
        for dept in departments
        if isinstance(dept, dict)
    )
    if args.dry_run:
        print(f"DRY-RUN: {legacy} → {canonical}（{len(departments)} 个部门 / {members} 名成员）")
        return 0
    save_org_payload({"departments": departments})
    # 校验读回。
    moved = load_org_payload().get("departments") or []
    print(f"OK: {canonical}（{len(moved)} 个部门）")
    print("提示：旧文件保留作回退；确认无误后可手工删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
