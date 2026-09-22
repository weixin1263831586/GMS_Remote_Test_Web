"""Redmine 组织架构与个人身份绑定 API（方案 2 读写分离）。

- 全局组织架构（部门 + 花名册）：人人可读，写仅限管理员（admin 角色，
  auth 关闭时放行）与人工会话（Agent token 一律 403）。canonical 存储
  ``configs/local/redmine_org_chart.json``。
- per-owner overlay（自我绑定 + 个人别名）：登录用户维护自己的，
  只影响自己的名字匹配/看板视角。
- 「添加成员」从 api.py 迁入：写的是全局组织架构，因此必须走管理员
  门禁；部门看板分组偏好（dashboard profiles）仍按 owner 存储。

身份解析优先级（resolve_owner_names 的快速路径）：overlay ``me`` 绑定
> Redmine 当前用户名/邮箱匹配合并视图 > 配置的用户名。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from features.auth import (
    require_human_principal,
    require_role_when_auth_required,
)
from features.users import owner_id_from_request

from .api import (
    _clear_stats_caches,
    _department_from_profiles,
    _department_ids_from_body,
    config_manager,
)
from .dashboard import (
    assign_user_to_profiles,
    denormalize_redmine_dashboard_config,
)
from .org_chart import (
    get_self_binding,
    load_org_payload,
    org_chart_is_legacy_path,
    org_chart_path,
    save_org_payload,
    set_member_aliases,
    set_self_binding,
    upsert_org_member,
)
from .users import display_names_from_mapping


router = APIRouter(prefix="/api/redmine-agent", tags=["redmine-org"])

__all__ = ["resolve_owner_names_with_binding", "router"]


def resolve_owner_names_with_binding(owner_id: str, fallback_names: list[str]) -> list[str]:
    """自我绑定优先的姓名解析：绑定存在时直接采用其展示名集合。"""
    try:
        member = get_self_binding(owner_id)
    except Exception:
        member = None
    if member:
        names = display_names_from_mapping(member)
        return list(dict.fromkeys([*names, *fallback_names]))
    return fallback_names


@router.get("/org-chart")
async def get_org_chart(request: Request):
    return {
        "success": True,
        "data": {
            "payload": load_org_payload(),
            "path": str(org_chart_path()),
            "legacy_import_available": org_chart_is_legacy_path(),
        },
    }


@router.put("/org-chart")
async def put_org_chart(
    request: Request,
    _admin: Any = Depends(require_role_when_auth_required("admin")),
):
    require_human_principal(request)
    body = await request.json()
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    if not isinstance(payload, dict) or not isinstance(payload.get("departments"), list):
        return {"success": False, "error": "payload.departments must be a list"}
    clean: dict[str, Any] = {"departments": payload.get("departments") or []}
    for key in ("updated_at", "note"):
        if isinstance(payload.get(key), str):
            clean[key] = payload[key]
    save_org_payload(clean)
    return {"success": True, "data": {"path": str(org_chart_path())}}


@router.post("/org-chart/import-legacy")
async def import_legacy_org_chart(
    request: Request,
    _admin: Any = Depends(require_role_when_auth_required("admin")),
):
    """把旧扁平 configs/redmine_user_map.json 导入 canonical 组织架构文件。"""
    require_human_principal(request)
    current = Path(org_chart_path())
    # canonical 缺失时 org_chart_path() 本身就解析到旧扁平文件，
    # 此时直接以其为导入源（parent.parent 推导只在 canonical 生效时成立）。
    legacy = (
        current
        if org_chart_is_legacy_path()
        else current.parent.parent / "redmine_user_map.json"
    )
    if not legacy.exists():
        return {"success": False, "error": "未找到旧版 configs/redmine_user_map.json"}
    try:
        payload = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"success": False, "error": f"旧文件解析失败: {exc}"}
    if not isinstance(payload, dict) or not isinstance(payload.get("departments"), list):
        return {"success": False, "error": "旧文件缺少 departments 列表"}
    save_org_payload({"departments": payload.get("departments") or []})
    return {
        "success": True,
        "data": {
            "path": str(org_chart_path()),
            "departments": len(payload.get("departments") or []),
        },
    }


@router.get("/me/binding")
async def get_my_binding(request: Request):
    # 读写都仅人工会话（ADR 0012）：个人身份绑定是账号级偏好，
    # Agent token / 机器能力 principal 不得读取或代写。
    require_human_principal(request)
    member = get_self_binding(owner_id_from_request(request))
    return {"success": True, "data": {"member": member}}


@router.put("/me/binding")
async def put_my_binding(request: Request):
    require_human_principal(request)
    body = await request.json()
    try:
        member = set_self_binding(owner_id_from_request(request), body.get("member_id"))
    except KeyError as exc:
        return {"success": False, "error": str(exc.args[0] if exc.args else exc)}
    return {"success": True, "data": {"member": member}}


@router.put("/me/aliases")
async def put_my_aliases(request: Request):
    require_human_principal(request)
    body = await request.json()
    aliases = body.get("aliases")
    if not isinstance(aliases, list):
        return {"success": False, "error": "aliases must be a list"}
    try:
        saved = set_member_aliases(owner_id_from_request(request), body.get("member_id"), aliases)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "data": {"member_id": body.get("member_id"), "aliases": saved}}


@router.post("/org-chart/members")
async def add_org_member(
    request: Request,
    _admin: Any = Depends(require_role_when_auth_required("admin")),
):
    """添加/更新组织架构成员（原 api.py POST /users 的全局写路径迁移）。"""
    require_human_principal(request)
    body = await request.json()
    uid = body.get("id")
    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip()
    department_ids = _department_ids_from_body(body)
    department = _department_from_profiles(department_ids)
    if not uid or not name:
        return {"success": False, "error": "id and name are required"}
    member: dict[str, Any] = {"id": uid, "name": name}
    if email:
        member["email"] = email
    result = upsert_org_member(member, department)
    if department_ids:
        dashboard_cfg = assign_user_to_profiles(
            config_manager.get_redmine_dashboard_config(),
            str(uid).strip(),
            department_ids,
        )
        if not config_manager.save_redmine_dashboard_config(
                denormalize_redmine_dashboard_config(dashboard_cfg)):
            return JSONResponse(status_code=500, content={
                "success": False, "error": "failed to save department membership"})
        _clear_stats_caches()
    return {"success": True, "data": result}
