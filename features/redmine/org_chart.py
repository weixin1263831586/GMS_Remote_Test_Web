"""Redmine 组织架构（共享）与个人 overlay（别名/自我绑定）的存储与合并。

方案 2 拆分（替代「整份 user_map 按 owner 复制」）：

- **全局组织架构**（部门 + 成员花名册）是全组共享的事实：人人读同一份，
  写仅限管理员。canonical 路径 ``configs/local/redmine_org_chart.json``，
  旧部署的扁平 ``configs/redmine_user_map.json`` 作为读兼容回退
  （``_prefer_existing``）。
- **per-owner overlay**（自我绑定 + 个人别名）沿用原 owner 文件路径
  ``data/redmine/by_user/<owner>/redmine_user_map.json``，但语义收窄为
  ``{"me": <member_id>|null, "aliases": {"<member_id>": ["别名"...]}}``。
  旧格式文件里的 ``departments`` 键被忽略——组织架构不再按 owner 复制，
  个人的部门看板分组偏好仍走 dashboard profiles（per-owner）。

合并视图 ``effective_user_map`` 是唯一读入口：statistics/dashboard/
daily-brief 等消费方拿到的仍是扁平成员列表（含 overlay 别名增强），
不需要感知双层存储。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foundation.config import settings
from foundation.config_paths import _prefer_existing, config_root

from .users import _flatten_departments, owner_user_map_path


__all__ = [
    "ORG_CHART_FILENAME",
    "effective_user_map",
    "get_self_binding",
    "load_org_payload",
    "load_user_overlay",
    "org_chart_is_legacy_path",
    "org_chart_path",
    "save_org_payload",
    "save_user_overlay",
    "set_member_aliases",
    "set_self_binding",
    "upsert_org_member",
]

ORG_CHART_FILENAME = "redmine_org_chart.json"
_LEGACY_ORG_FILENAME = "redmine_user_map.json"


def org_chart_path(project_root: Path | str | None = None) -> Path:
    """全局组织架构路径；canonical 缺失时回退读旧扁平 user_map。"""
    root = Path(project_root) if project_root else Path(settings.project_root)
    canonical = config_root(root) / "local" / ORG_CHART_FILENAME
    legacy = config_root(root) / _LEGACY_ORG_FILENAME
    return _prefer_existing(canonical, legacy)


def org_chart_is_legacy_path() -> bool:
    """当前生效的组织架构文件是否还是旧扁平 user_map（待导入）。"""
    return org_chart_path().name == _LEGACY_ORG_FILENAME


def _empty_payload() -> dict[str, Any]:
    return {"departments": []}


def load_org_payload(project_root: Path | str | None = None) -> dict[str, Any]:
    path = org_chart_path(project_root)
    if not path.exists():
        return _empty_payload()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_payload()
    if not isinstance(payload, dict):
        return _empty_payload()
    payload.setdefault("departments", [])
    return payload


def save_org_payload(payload: dict[str, Any], project_root: Path | str | None = None) -> None:
    path = org_chart_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_user_overlay(owner_id: str) -> dict[str, Any]:
    """读 overlay；旧格式（含 departments）容忍并忽略组织架构键。"""
    path = owner_user_map_path(owner_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    payload.pop("departments", None)
    return payload


def save_user_overlay(owner_id: str, overlay: dict[str, Any]) -> None:
    overlay.pop("departments", None)
    path = owner_user_map_path(owner_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")


def _overlay_aliases(overlay: dict[str, Any], member_id: Any) -> list[str]:
    raw = (overlay.get("aliases") or {}).get(str(member_id)) or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def effective_user_map(owner_id: str) -> list[dict[str, Any]]:
    """合并视图：全局组织架构成员 + 该 owner 的个人别名增强。

    匹配（find_user_mapping_for_names）与 UI 列表都消费这份扁平列表；
    overlay 里指向不存在成员的别名自然落空（不报错）。
    """
    members = _flatten_departments(load_org_payload())
    overlay = load_user_overlay(owner_id)
    if not overlay:
        return members
    for member in members:
        extra = _overlay_aliases(overlay, member.get("id"))
        if not extra:
            continue
        merged = list(dict.fromkeys(
            [*(member.get("aliases") or []), *extra]))
        member["aliases"] = merged
    return members


def get_self_binding(owner_id: str) -> dict[str, Any] | None:
    """返回 overlay ``me`` 指向的组织成员（不存在/未绑定 → None）。"""
    overlay = load_user_overlay(owner_id)
    member_id = overlay.get("me")
    if member_id is None or str(member_id).strip() == "":
        return None
    wanted = str(member_id).strip()
    for member in _flatten_departments(load_org_payload()):
        if str(member.get("id") or "").strip() == wanted:
            return member
    return None


def set_self_binding(owner_id: str, member_id: Any) -> dict[str, Any] | None:
    """绑定/解绑（member_id=None）自我身份；目标必须是组织架构现存成员。"""
    overlay = load_user_overlay(owner_id)
    if member_id is None or str(member_id).strip() == "":
        overlay["me"] = None
        save_user_overlay(owner_id, overlay)
        return None
    wanted = str(member_id).strip()
    member = next(
        (item for item in _flatten_departments(load_org_payload())
         if str(item.get("id") or "").strip() == wanted),
        None,
    )
    if member is None:
        raise KeyError("组织架构中不存在该成员")
    overlay["me"] = member.get("id")
    save_user_overlay(owner_id, overlay)
    return member


def set_member_aliases(owner_id: str, member_id: Any, aliases: list[str]) -> list[str]:
    """维护个人别名（覆盖式）；空列表清除该成员的别名。"""
    wanted = str(member_id or "").strip()
    if not wanted:
        raise ValueError("member_id is required")
    cleaned = list(dict.fromkeys(
        str(item).strip() for item in (aliases or []) if str(item).strip()))
    overlay = load_user_overlay(owner_id)
    alias_map = overlay.setdefault("aliases", {})
    if isinstance(alias_map, dict):
        if cleaned:
            alias_map[wanted] = cleaned
        else:
            alias_map.pop(wanted, None)
        if not alias_map:
            overlay.pop("aliases", None)
    save_user_overlay(owner_id, overlay)
    return cleaned


def upsert_org_member(
    member: dict[str, Any],
    department: dict[str, str],
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """新增/更新全局组织架构成员（管理员路径）。

    ``department`` 是端点侧解析好的 ``{"department_id", "department"}``
    （profile id → 名称，与原 per-owner 端点同语义）。同 id 成员跨部门
    去重（保留目标部门一份），部门不存在则创建。
    返回 ``{"created": bool, "department_ids": [...]}``。
    """
    payload = load_org_payload(project_root)
    departments = payload.setdefault("departments", [])
    uid = str(member.get("id") or "").strip()
    dept_id = str(department.get("department_id") or "").strip()
    dept_name = str(department.get("department") or "").strip()
    created = True
    target_department = None
    for dept in departments:
        if not isinstance(dept, dict):
            continue
        if dept_id and str(dept.get("department_id") or "").strip() == dept_id:
            target_department = dept
            break
        if not dept_id and dept_name and str(dept.get("department") or "").strip() == dept_name:
            target_department = dept
            break
    if target_department is None:
        target_department = {"department_id": dept_id, "department": dept_name, "members": []}
        departments.append(target_department)
    updated_member = dict(member)
    for dept in departments:
        if not isinstance(dept, dict):
            continue
        members = dept.setdefault("members", [])
        kept = []
        for item in members:
            if isinstance(item, dict) and str(item.get("id") or "").strip() == uid:
                created = False
                if dept is target_department:
                    kept.append(updated_member)
                    updated_member = None  # 已插入目标位置
                continue
            kept.append(item)
        dept["members"] = kept
    if updated_member is not None:
        # created（新成员）或「更新但目标部门原无此成员」（换部门）都要落位。
        target_department.setdefault("members", []).append(updated_member)
    payload.pop("users", None)
    save_org_payload(payload, project_root)
    return {"created": created, "department_ids": [dept_id] if dept_id else []}
