"""Per-user device group normalization, persistence, and routes."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from features.auth import get_authenticated_user
from foundation.responses import error_response, success_response

from . import runtime
from .clients import get_client_id_from_request


router = APIRouter()
_storage_lock = threading.RLock()

_DEVICE_GROUP_COLORS = (
    '#3b82f6',
    '#764ba2',
    '#10b981',
    '#f59e0b',
    '#ef4444',
    '#06b6d4',
    '#ec4899',
)
_AUTO_DIM_TO_PROP = {
    'model': 'model',
    'android_version': 'android_version',
    'soc': 'soc_model',
    'worker': 'source_host',
}


def _default_group_color(index: int) -> str:
    return _DEVICE_GROUP_COLORS[index % len(_DEVICE_GROUP_COLORS)]


def normalize_device_groups(raw: Any) -> list[dict[str, Any]]:
    """Validate and normalize device group definitions."""
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail='groups 必须是数组')

    normalized = []
    seen_ids = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        group_id = str(item.get('id') or '').strip()
        name = str(item.get('name') or '').strip()
        if not group_id or group_id in seen_ids or not name:
            continue
        seen_ids.add(group_id)

        device_ids = _coerce_device_ids(item.get('device_ids'))
        normalized.append(
            {
                'id': group_id,
                'name': name,
                'color': str(item.get('color') or '').strip()
                or _default_group_color(index),
                'device_ids': device_ids,
                'followed': bool(item.get('followed', False)),
            }
        )
    return normalized


def build_device_group_map(
    groups: list[dict[str, Any]],
) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for group in groups:
        for device_id in group.get('device_ids', []):
            mapping.setdefault(device_id, []).append(group['id'])
    return mapping


def soc_series(value: str) -> str:
    """Collapse an SoC variant suffix, e.g. RK3588S to RK3588."""
    return re.sub(r'[A-Za-z]+$', '', value).strip() or value


def _automatic_group_id(dimension: str, value: str) -> str:
    suffix = re.sub(r'[^a-zA-Z0-9_-]+', '_', value).strip('_') or 'unknown'
    return f'auto_{dimension}_{suffix}'


def auto_assign_new_devices(
    username: str | None,
    device_props: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Append devices and host/property values to persisted automatic groups."""
    groups = load_device_groups(username)
    if not device_props:
        return groups

    auto_rules: dict[tuple[str, str], dict[str, Any]] = {}
    active_dimensions: set[str] = set()
    for group in groups:
        if not str(group.get('id', '')).startswith('auto_'):
            continue
        dimension, separator, value = str(group.get('name', '')).partition(': ')
        if separator and dimension in _AUTO_DIM_TO_PROP:
            auto_rules[(dimension, value)] = group
            active_dimensions.add(dimension)
    if not auto_rules:
        return groups

    changed = False
    known_ids = {str(group.get('id') or '') for group in groups}
    for device_id, properties in device_props.items():
        for dimension in active_dimensions:
            raw = str(properties.get(_AUTO_DIM_TO_PROP[dimension]) or '').strip()
            if not raw:
                continue
            current = soc_series(raw) if dimension == 'soc' else raw
            rule_key = (dimension, current)
            group = auto_rules.get(rule_key)
            if group is None:
                group_id = _automatic_group_id(dimension, current)
                if group_id in known_ids:
                    digest = hashlib.sha256(
                        f'{dimension}:{current}'.encode()
                    ).hexdigest()[:8]
                    group_id = f'{group_id}_{digest}'
                group = {
                    'id': group_id,
                    'name': f'{dimension}: {current}',
                    'color': _default_group_color(len(groups)),
                    'device_ids': [],
                    'followed': False,
                }
                groups.append(group)
                known_ids.add(group_id)
                auto_rules[rule_key] = group
                changed = True
            device_ids = group.get('device_ids') or []
            if device_id not in device_ids:
                device_ids.append(device_id)
                group['device_ids'] = device_ids
                changed = True

    if changed:
        groups = normalize_device_groups(groups)
        save_device_groups(username, groups)
    return groups


def cluster_device_properties(service: Any = None) -> dict[str, dict[str, str]]:
    """Return online remote Worker devices in the management/grouping shape."""
    try:
        if service is None:
            from features.users.cluster_access import get_cluster_service

            service = get_cluster_service()
        if service is None or not service.effective_enabled:
            return {}
        local_worker_id = service.config.local_worker_id
        worker_names = {
            worker['id']: worker.get('name') or worker['id']
            for worker in service.list_workers()
        }
        devices: dict[str, dict[str, str]] = {}
        for device in service.repository.list_devices():
            worker_id = str(device.get('worker_id') or '')
            if (
                not worker_id
                or worker_id == local_worker_id
                or device.get('state') in {'offline', 'unknown'}
            ):
                continue
            raw = device.get('properties') or {}
            properties = {
                str(key): str(value)
                for key, value in raw.items()
                if value is not None
            }
            properties['model'] = (
                properties.get('model') or properties.get('product') or ''
            )
            properties['source_host'] = worker_names.get(worker_id, worker_id)
            devices[str(device['id'])] = properties
        return devices
    except Exception:
        # Device groups remain usable in single-host mode and during cluster
        # bootstrap/offline periods.
        return {}


def current_username_for_request(request: Request) -> str:
    """Return the authenticated RESOURCE owner or the anonymous client id.

    Device groups are per-user preferences (account resources, ADR 0010):
    key them by ``resource_owner_id`` so an agent token and its enrolling
    account (or an ATS run and its creator) share one group layout instead
    of fragmenting per synthetic actor id on every rotation.
    """
    user = get_authenticated_user(request)
    return user.resource_owner_id if user else get_client_id_from_request(request)


def _owner_storage_key(username: str) -> str:
    raw = str(username or '').strip()
    key = ''.join(
        character if character.isalnum() or character in {'-', '_'} else '_'
        for character in raw
    )
    if not key:
        return 'anonymous'
    if key != raw:
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]
        return f'{key}_{digest}'
    return key


def _device_groups_path(username: str) -> Path:
    data_root = Path(runtime.data_root)
    directory = data_root / 'user_prefs' / _owner_storage_key(username)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / 'device_groups.json'


def _agent_token_ids_for_owner(owner: str) -> list[str]:
    """Token ids enrolled by this account (cross-feature public surface)."""
    try:
        from features.auth import auth_service

        records = auth_service.list_agent_tokens()
    except Exception:  # 注册表不可用时按无历史数据处理（迁移是尽力而为）
        return []
    return [
        str(record.get('id') or '')
        for record in records
        if str(record.get('owner_user_id') or '') == str(owner)
    ]


def _migrate_legacy_agent_groups(username: str, target: Path) -> None:
    """One-time lazy migration for the ADR 0010 owner-key switch.

    设备分组曾按 ``user.id`` 落盘：人类账号 id 不变无需迁移；agent 令牌
    的合成 actor id（``agent:<token_id>``）在切换后会让账号看不到旧分
    组。这里借 agent_tokens 注册表把该账号名下每个 token 的历史 key 反
   查出来，取 mtime 最新的分组文件复制到账号 key 下。幂等：仅在目标
    文件缺失时执行；任何失败都按"无历史数据"处理（fail-open）。
    """
    if target.is_file():
        return
    data_root = Path(runtime.data_root) / 'user_prefs'
    candidates = []
    for token_id in _agent_token_ids_for_owner(username):
        if not token_id:
            continue
        legacy = (
            data_root
            / _owner_storage_key(f'agent:{token_id}')
            / 'device_groups.json'
        )
        try:
            if legacy.is_file():
                candidates.append(legacy)
        except OSError:
            continue
    if not candidates:
        return
    legacy_path = max(candidates, key=lambda candidate: candidate.stat().st_mtime)
    try:
        data = json.loads(legacy_path.read_text(encoding='utf-8'))
        groups = normalize_device_groups(
            data.get('groups', []) if isinstance(data, dict) else data
        )
    except (OSError, json.JSONDecodeError, HTTPException):
        return
    if not groups:
        return
    try:
        temporary = target.with_suffix(f'{target.suffix}.tmp')
        temporary.write_text(
            json.dumps({'groups': groups}, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        temporary.replace(target)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_device_groups(username: str | None) -> list[dict[str, Any]]:
    """Load groups for one immutable authenticated owner id."""
    if not username:
        return []

    path = _device_groups_path(username)
    with _storage_lock:
        _migrate_legacy_agent_groups(username, path)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return normalize_device_groups(
                data.get('groups', []) if isinstance(data, dict) else data
            )
        except (OSError, json.JSONDecodeError, HTTPException):
            return []


def save_device_groups(
    username: str | None,
    groups: list[dict[str, Any]],
) -> bool:
    if not username:
        return False

    path = _device_groups_path(username)
    temporary = path.with_suffix(f'{path.suffix}.tmp')
    with _storage_lock:
        try:
            temporary.write_text(
                json.dumps({'groups': groups}, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            temporary.replace(path)
            return True
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False


@router.get('/api/device-groups')
async def get_device_groups(request: Request):
    """获取当前用户的设备分组定义。"""
    username = current_username_for_request(request)
    groups = auto_assign_new_devices(username, cluster_device_properties())
    return success_response({'groups': groups})


@router.post('/api/device-groups')
async def mutate_device_groups(
    request: Request,
    req: dict = Body(default={}),
):
    """设备分组增删改 / 重排 / 分配设备（per-user）。

    action:
      create  {name, color?, device_ids?, followed?}      -> 新建分组（id 后端生成）
      update  {id, name?, color?, device_ids?, followed?} -> 更新分组字段
      delete  {id}                                         -> 删除分组（其设备归未分组）
      reorder {ids: [id,...]}                              -> 按给定顺序重排
      assign  {id, device_ids, mode: "set"|"add"|"remove"}-> 设置/追加/移除组内设备
    """
    action = str(req.get('action') or '').strip()
    if action not in {'create', 'update', 'delete', 'reorder', 'assign'}:
        return error_response(
            'action 必须是 create/update/delete/reorder/assign',
            status_code=400,
        )

    username = current_username_for_request(request)
    # 串行执行完整读改写事务，避免并发覆盖。
    return _mutate_device_groups(username, req, action)


def _mutate_device_groups(
    username: str | None,
    req: dict[str, Any],
    action: str,
):
    with _storage_lock:
        return _mutate_device_groups_locked(username, req, action)


def _mutate_device_groups_locked(
    username: str | None,
    req: dict[str, Any],
    action: str,
):
    groups = load_device_groups(username)

    if action == 'create':
        name = str(req.get('name') or '').strip()
        if not name:
            return error_response('分组名称不能为空', status_code=400)
        group_id = _gen_group_id(groups)
        device_ids = _coerce_device_ids(req.get('device_ids'))
        groups.append(
            {
                'id': group_id,
                'name': name,
                'color': str(req.get('color') or '').strip()
                or _default_group_color(len(groups)),
                'device_ids': device_ids,
                'followed': bool(req.get('followed', False)),
            }
        )
        enforce_exclusive_device_group(groups, group_id, device_ids)
    elif action == 'update':
        group = _find_group(groups, req.get('id'))
        if not group:
            return error_response('分组不存在', status_code=404)
        invalid = _update_group(groups, group, req)
        if invalid:
            return invalid
    elif action == 'delete':
        group_id = str(req.get('id') or '').strip()
        groups = [group for group in groups if group['id'] != group_id]
    elif action == 'reorder':
        group_ids = [
            item.strip()
            for item in (req.get('ids') or [])
            if isinstance(item, str)
        ]
        by_id = {group['id']: group for group in groups}
        groups = [by_id[item] for item in group_ids if item in by_id] + [
            group for group in groups if group['id'] not in group_ids
        ]
    else:
        invalid = _assign_devices(groups, req)
        if invalid:
            return invalid

    if not save_device_groups(username, groups):
        return error_response('保存设备分组失败', status_code=500)
    return success_response({'groups': groups})


def _update_group(
    groups: list[dict[str, Any]],
    group: dict[str, Any],
    request: dict[str, Any],
):
    if 'name' in request:
        name = str(request.get('name') or '').strip()
        if not name:
            return error_response('分组名称不能为空', status_code=400)
        group['name'] = name
    if 'color' in request:
        color = str(request.get('color') or '').strip()
        if color:
            group['color'] = color
    if 'device_ids' in request:
        group['device_ids'] = _coerce_device_ids(request.get('device_ids'))
        enforce_exclusive_device_group(groups, group['id'], group['device_ids'])
    if 'followed' in request:
        group['followed'] = bool(request.get('followed'))
    return None


def _assign_devices(groups: list[dict[str, Any]], request: dict[str, Any]):
    group = _find_group(groups, request.get('id'))
    if not group:
        return error_response('分组不存在', status_code=404)
    mode = str(request.get('mode') or 'set').strip()
    incoming = _coerce_device_ids(request.get('device_ids'))
    incoming_set = set(incoming)
    if mode == 'set':
        group['device_ids'] = incoming
        exclusive_ids = group['device_ids']
    elif mode == 'add':
        existing = set(group['device_ids'])
        group['device_ids'].extend(
            device_id for device_id in incoming if device_id not in existing
        )
        exclusive_ids = incoming
    elif mode == 'remove':
        group['device_ids'] = [
            device_id
            for device_id in group['device_ids']
            if device_id not in incoming_set
        ]
        return None
    else:
        return error_response('mode 必须是 set/add/remove', status_code=400)
    enforce_exclusive_device_group(groups, group['id'], exclusive_ids)
    return None


def _coerce_device_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    result = []
    seen = set()
    for device_id in raw:
        if not isinstance(device_id, str):
            continue
        device_id = device_id.strip()
        if device_id and device_id not in seen:
            seen.add(device_id)
            result.append(device_id)
    return result


def enforce_exclusive_device_group(
    groups: list[dict[str, Any]],
    owner_id: str,
    device_ids: list[str],
) -> None:
    owned = set(device_ids)
    for group in groups:
        if group['id'] != owner_id:
            group['device_ids'] = [
                item for item in group.get('device_ids', []) if item not in owned
            ]


def _find_group(
    groups: list[dict[str, Any]],
    group_id: Any,
) -> dict[str, Any] | None:
    expected = str(group_id or '').strip()
    return next((group for group in groups if group['id'] == expected), None)


def _gen_group_id(existing: list[dict[str, Any]]) -> str:
    taken = {group['id'] for group in existing}
    while True:
        group_id = 'g_' + secrets.token_hex(3)
        if group_id not in taken:
            return group_id
