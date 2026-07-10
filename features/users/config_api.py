"""Configuration and Tailscale routes - config CRUD, sidebar order, Tailscale status."""

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from foundation.responses import error_response, success_response

from . import runtime
from .clients import (
    get_client_display_id_from_request,
    get_client_id_from_request,
    hide_sensitive_info,
    parse_client_id,
)


config_manager = runtime.config_manager
_SSH_DEVICE_HOST_RE = re.compile(r"^[^@\s<>\"'`]+@[^@\s<>\"'`]+$")


def get_effective_local_server(
    client_id: str,
    requested_local_server: str = "",
    request: Request | None = None,
) -> str:
    if requested_local_server:
        return requested_local_server
    runtime_config = config_manager.get_runtime_config()
    runtime_local_server = str(runtime_config.get("local_server") or "").strip()
    if "@" in runtime_local_server:
        return runtime_local_server
    if request is not None:
        display_id = get_client_display_id_from_request(request)
        if "@" in display_id:
            return display_id
    return client_id

logger = logging.getLogger(__name__)

router = APIRouter()

SIDEBAR_PAGES = {
    'test',
    'desktop',
    'terminal',
    'users',
    'devices',
    'reports',
    'report-analysis',
    'apk-analysis',
    'test-suites',
    'api-docs',
    'architecture',
    'websites',
    'tools',
    'security-audit',
    'gms-assistant',
    'automation',
    'redmine-agent',
    'gerrit-dashboard',
    'agent',
}


# ==================== Tailscale helpers ====================

def _get_tailscale_status() -> dict:
    """Get Tailscale IP and connection status via `tailscale ip -4`."""
    try:
        result = subprocess.run(
            ['tailscale', 'ip', '-4'],
            capture_output=True, text=True, timeout=3
        )
    except FileNotFoundError:
        return {'ip': None, 'connected': False, 'error': 'tailscale 命令未找到，请先安装 Tailscale'}
    except subprocess.TimeoutExpired:
        return {'ip': None, 'connected': False, 'error': 'tailscale status 超时'}

    if result.returncode == 0:
        ip = result.stdout.strip()
        if ip:
            return {'ip': ip, 'connected': True}

    error = (result.stderr or '').strip() or 'tailscale 未连接'
    return {'ip': None, 'connected': False, 'error': error}


def _build_tailscale_url(ip: str, request: Request | None = None) -> str:
    """Build Tailscale access URL from IP and GMS port."""
    port = os.environ.get('GMS_PORT', '5001')
    scheme = (
        os.environ.get('GMS_TAILSCALE_SCHEME')
        or os.environ.get('GMS_PUBLIC_SCHEME')
        or (request.headers.get('x-forwarded-proto') if request else None)
        or (request.url.scheme if request else None)
        or 'http'
    )
    scheme = str(scheme).split(',', 1)[0].strip().lower()
    if scheme not in {'http', 'https'}:
        scheme = 'http'
    return f'{scheme}://{ip}:{port}'


def normalize_sidebar_order(raw_order: Any) -> list[str]:
    """校验并去重侧边栏排序，丢弃不属于当前页面的历史残留名。"""
    if not isinstance(raw_order, list):
        raise HTTPException(status_code=400, detail="order 必须是数组")

    order = []
    seen = set()
    for item in raw_order:
        if not isinstance(item, str):
            continue
        page = item.strip()
        # 只保留当前真实存在的页面，过滤掉重构改名前的残留（如 ai-assistant）
        if page and page in SIDEBAR_PAGES and page not in seen:
            order.append(page)
            seen.add(page)

    if not order:
        raise HTTPException(status_code=400, detail="order 不能为空")
    return order


def normalize_sidebar_visible_pages(raw_pages: Any) -> list[str]:
    """校验并去重侧边栏可见页面。空数组表示使用默认全量可见。"""
    if not isinstance(raw_pages, list):
        return []

    pages = []
    seen = set()
    for item in raw_pages:
        if not isinstance(item, str):
            continue
        page = item.strip()
        if page in SIDEBAR_PAGES and page not in seen:
            pages.append(page)
            seen.add(page)
    return pages


_tailscale_start_lock = asyncio.Lock()


# 设备分组配色盘（与品牌渐变一致的几色）
_DEVICE_GROUP_COLORS = ("#3b82f6", "#764ba2", "#10b981", "#f59e0b", "#ef4444", "#06b6d4", "#ec4899")


def _default_group_color(index: int) -> str:
    """按索引循环取一个默认配色。"""
    return _DEVICE_GROUP_COLORS[index % len(_DEVICE_GROUP_COLORS)]


def normalize_device_groups(raw: Any) -> list[dict[str, Any]]:
    """校验设备分组定义，返回规整后的 groups 列表。

    每个分组形如 {"id","name","color","device_ids": [...]}。设备序列号去重；
    允许一台设备同时属于多个分组（OR 语义）。非法项被静默丢弃而非整体失败。
    """
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="groups 必须是数组")

    normalized = []
    seen_ids = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue

        group_id = str(item.get("id") or "").strip()
        if not group_id or group_id in seen_ids:
            continue
        seen_ids.add(group_id)

        name = str(item.get("name") or "").strip()
        if not name:
            continue

        color = str(item.get("color") or "").strip() or _default_group_color(idx)

        device_ids_raw = item.get("device_ids", [])
        device_ids = []
        seen_dev = set()
        if isinstance(device_ids_raw, list):
            for dev in device_ids_raw:
                if not isinstance(dev, str):
                    continue
                dev = dev.strip()
                if dev and dev not in seen_dev:
                    device_ids.append(dev)
                    seen_dev.add(dev)

        normalized.append(
            {
                "id": group_id,
                "name": name,
                "color": color,
                "device_ids": device_ids,
                "followed": bool(item.get("followed", False)),
            }
        )
    return normalized


def build_device_group_map(groups: list[dict[str, Any]]) -> dict[str, list[str]]:
    """由 groups 列表反查 device_id -> [group_id, ...] 映射（供 /devices/list join）。"""
    mapping: dict[str, list[str]] = {}
    for group in groups:
        for dev in group.get("device_ids", []):
            mapping.setdefault(dev, []).append(group["id"])
    return mapping


def soc_series(value: str) -> str:
    """归并 SOC 型号到数字系列：去掉末尾的纯字母后缀。

    RK3576S -> RK3576，RK3588S -> RK3588；RK3576/MSM8953 等无字母后缀的保持不变，
    以便同系列设备归到同一个分组。生成(auto_group)与补全(auto_assign_new_devices)共用。
    """
    return re.sub(r"[A-Za-z]+$", "", value).strip() or value


# 自动分组维度 -> 设备属性键（与 features/devices/api.py 的 _AUTO_GROUP_KEYS 保持一致）
_AUTO_DIM_TO_PROP = {
    "model": "model",
    "android_version": "android_version",
    "soc": "soc_model",
}


def auto_assign_new_devices(
    username: str | None,
    device_props: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """把匹配 auto 分组规则但尚未在组内的设备补进去（只增不减，不互斥）。

    device_props: {device_id: {"soc_model": ..., "model": ..., "android_version": ...}}，
    通常来自设备管理端点的批量 getprop 解析结果。

    auto 分组的规则由组名 "{dim}: {value}" 反推（dim ∈ model/android_version/soc）；
    soc 维度的设备值会先做系列归并（soc_series）再比较。仅往匹配的 auto 组追加
    新设备，不触碰手建组，也不在 auto 组间做互斥（允许一台设备属于多个 auto 组）。
    有变更时持久化并返回新列表，否则原样返回。
    """
    groups = load_device_groups(username)
    if not device_props:
        return groups

    # 预解析每个 auto 组的 (dim, value) 规则；格式不符的跳过
    auto_rules: list[tuple[dict[str, Any], str, str]] = []
    for g in groups:
        if not str(g.get("id", "")).startswith("auto_"):
            continue
        dim, sep, value = str(g.get("name", "")).partition(": ")
        if not sep or dim not in _AUTO_DIM_TO_PROP:
            continue
        auto_rules.append((g, dim, value))
    if not auto_rules:
        return groups

    changed = False
    for g, dim, target_value in auto_rules:
        prop_key = _AUTO_DIM_TO_PROP[dim]
        ids = g.get("device_ids") or []
        existing = set(ids)
        for device_id, props in device_props.items():
            if device_id in existing:
                continue
            raw = str(props.get(prop_key) or "").strip()
            if not raw:
                continue
            cur = soc_series(raw) if dim == "soc" else raw
            if cur == target_value:
                ids.append(device_id)
                existing.add(device_id)
                changed = True
        if changed and ids is not g.get("device_ids"):
            g["device_ids"] = ids

    if changed:
        save_device_groups(username, groups)
    return groups



# ==================== Routes ====================

@router.get("/api/config/read")
async def get_config(request: Request):
    """获取配置 - 隐藏敏感信息后返回配置对象"""
    # 跟踪用户访问
    client_id = get_client_id_from_request(request)
    runtime.get_or_create_user_state(client_id)

    config = config_manager.load_config()
    # local_server 是客户端回传地址；没有显式动态配置时，按当前请求用户/IP 展示。
    config['local_server'] = get_effective_local_server(client_id, request=request)

    # 隐藏敏感信息
    safe_config = hide_sensitive_info(config.copy())
    return JSONResponse(content=safe_config)


@router.get("/api/config/opengrok")
async def get_opengrok_config(request: Request):
    """获取OpenGrok配置 - 供前端源码链接使用"""
    config = config_manager.load_config()
    opengrok_config = config.get('opengrok', {})

    if not opengrok_config or 'base_url' not in opengrok_config:
        return error_response('OpenGrok未配置，请在configs/config.json中配置opengrok段', status_code=404)

    return success_response(opengrok_config)


@router.get("/api/config/ai")
async def get_ai_config(request: Request):
    """获取 AI 配置 - 供前端 AI 分析功能使用"""
    ai_config = config_manager.get_ai_config()

    if not ai_config:
        return error_response('AI 未配置或未启用，请在 configs/config.json 中配置 ai_models 段并设置 enabled: true', status_code=404)

    return success_response(hide_sensitive_info(ai_config.copy()))


@router.get("/api/tailscale/status")
async def get_tailscale_status(request: Request):
    """获取 Tailscale 内网访问地址"""
    try:
        status = await asyncio.to_thread(_get_tailscale_status)
    except Exception as e:
        return error_response(f'无法获取 Tailscale 信息：{e!s}', status_code=503)

    if status.get('ip'):
        url = _build_tailscale_url(status['ip'], request)
        return JSONResponse(content={'success': True, 'public_url': url, 'connected': status.get('connected', False)})

    return error_response('Tailscale 未连接或未安装', status_code=404)


@router.post("/api/tailscale/ensure")
async def ensure_tailscale_url(request: Request):
    """检查 Tailscale 连接状态，未连接时尝试启动，返回内网访问地址。"""
    status = await asyncio.to_thread(_get_tailscale_status)

    if status.get('ip'):
        url = _build_tailscale_url(status['ip'], request)
        return JSONResponse(content={
            'success': True,
            'public_url': url,
            'connected': status.get('connected', False)
        })

    # Tailscale 未连接，尝试自动启动（需要 sudoers 免密配置）
    async with _tailscale_start_lock:
        # 双重检查：可能上一个请求已经启动成功
        status = await asyncio.to_thread(_get_tailscale_status)
        if status.get('ip'):
            return JSONResponse(content={
                'success': True,
                'public_url': _build_tailscale_url(status['ip'], request),
                'connected': status.get('connected', False)
            })
        try:
            # 先检查 tailscaled 服务是否运行，未运行则启动
            svc_check = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "is-active", "--quiet", "tailscaled"],
                capture_output=True, text=True, timeout=5
            )
            if svc_check.returncode != 0:
                # 尝试启动 tailscaled（需要 sudoers 免密）
                await asyncio.to_thread(
                    subprocess.run,
                    ["sudo", "systemctl", "enable", "--now", "tailscaled"],
                    capture_output=True, text=True, timeout=15
                )
                await asyncio.sleep(2)

            # 尝试获取 IP（可能已经 authenticated）
            status = await asyncio.to_thread(_get_tailscale_status)
            if status.get('ip'):
                return JSONResponse(content={
                    'success': True,
                    'public_url': _build_tailscale_url(status['ip'], request),
                    'connected': status.get('connected', False)
                })

            # 未 authenticated，需要用户手动登录
            return error_response(
                'Tailscale 已安装但未连接。请在终端执行 sudo tailscale up 完成账号授权，'
                '或确认 sudoers 已配置 Tailscale 免密命令。',
                status_code=503
            )
        except Exception as e:
            return error_response(f'Tailscale 启动失败：{e!s}', status_code=503)


@router.post("/api/config/update")
async def update_config(req: dict):
    """更新配置 - 只修改运行时配置，禁止修改config.json"""
    existing_runtime = config_manager.get_runtime_config()

    # 运行时配置字段（保存在 config_runtime.json）
    # 注意：client_ip 和 client_username 是运行时状态，不应保存到配置文件
    runtime_keys = {
        'client_hosts', 'client_ssh_credentials', 'local_server', 'sidebar_order', 'sidebar_visible_pages'
    }

    # 检查是否有不允许修改的字段
    invalid_fields = set(req.keys()) - runtime_keys
    if invalid_fields:
        return error_response(
            f"不允许修改以下字段: {', '.join(invalid_fields)}. 可修改的字段: {', '.join(runtime_keys)}",
            status_code=400,
        )

    # 只更新允许字段，保留 Gerrit/Redmine/USB-IP 等其他运行时段落。
    runtime_updates = dict(existing_runtime)
    for key in runtime_keys:
        if key in req:
            runtime_updates[key] = req[key]

    # 保存运行时配置
    if config_manager.save_runtime_config(runtime_updates):
        return success_response()
    else:
        return error_response("保存配置失败", status_code=500)


# ==================== Client SSH Credentials ====================

def _public_credentials(credentials: list) -> list:
    """返回凭据列表的脱敏副本（密码替换为 ***，永不回传明文）。"""
    public = []
    for cred in credentials or []:
        if not isinstance(cred, dict):
            continue
        item = {
            "device_host": str(cred.get("device_host") or "").strip(),
            "username": str(cred.get("username") or "").strip(),
            "host": str(cred.get("host") or cred.get("hostname") or "").strip(),
            "has_password": bool(cred.get("password")),
        }
        public.append(item)
    return public


def _validate_ssh_device_host(device_host: str) -> bool:
    return bool(_SSH_DEVICE_HOST_RE.fullmatch(device_host or ""))


@router.get("/api/config/client-ssh-credentials")
async def list_client_ssh_credentials():
    """列出已保存的客户端 SSH 凭据（密码脱敏）。"""
    runtime_cfg = config_manager.get_runtime_config()
    credentials = runtime_cfg.get("client_ssh_credentials") or []
    if not isinstance(credentials, list):
        credentials = []
    return success_response({"credentials": _public_credentials(credentials)})


@router.post("/api/config/client-ssh-credentials")
async def upsert_client_ssh_credential(req: dict = Body(default={})):
    """新增或更新一条客户端 SSH 凭据（按 device_host 增改）。"""
    device_host = str(req.get("device_host") or "").strip()
    password = str(req.get("password") or "")
    if not _validate_ssh_device_host(device_host):
        return error_response(
            "设备主机格式错误，应为 user@ip，例如 gms@192.168.1.100",
            status_code=400,
        )
    if not password:
        return error_response("密码不能为空", status_code=400)

    if config_manager.upsert_device_host_password(device_host, password):
        return success_response(message="凭据已保存")
    return error_response("保存凭据失败", status_code=500)


@router.delete("/api/config/client-ssh-credentials")
async def delete_client_ssh_credential(req: dict = Body(default={})):
    """删除一条客户端 SSH 凭据（按 device_host 或 username@host 匹配）。"""
    device_host = str(req.get("device_host") or "").strip()
    if not _validate_ssh_device_host(device_host):
        return error_response(
            "设备主机格式错误，应为 user@ip，例如 gms@192.168.1.100",
            status_code=400,
        )
    username, hostname = parse_client_id(device_host)

    runtime_cfg = config_manager.get_runtime_config()
    credentials = runtime_cfg.get("client_ssh_credentials") or []
    if not isinstance(credentials, list):
        credentials = []

    remaining = []
    for cred in credentials:
        if not isinstance(cred, dict):
            continue
        cred_device_host = str(cred.get("device_host") or "").strip()
        cred_username = str(cred.get("username") or "").strip()
        cred_host = str(cred.get("host") or cred.get("hostname") or "").strip()
        is_same = (
            (cred_device_host and cred_device_host == device_host)
            or (cred_username == username and cred_host == hostname)
        )
        if not is_same:
            remaining.append(cred)

    if config_manager.save_client_ssh_credentials(remaining):
        return success_response(message="凭据已删除")
    return error_response("删除凭据失败", status_code=500)


@router.get("/api/sidebar-order")
async def get_sidebar_order():
    """获取侧边栏导航顺序。"""
    existing_runtime = config_manager.get_runtime_config()
    order = existing_runtime.get('sidebar_order', [])
    if not isinstance(order, list):
        order = []
    # 过滤掉重构改名前的历史残留页名（如 ai-assistant），否则前端 F5 重排时
    # 会把不存在的页面当成排序键，导致真实页面被挤到末尾、导航栏乱跳。
    order = [page for page in order if isinstance(page, str) and page in SIDEBAR_PAGES]
    visible_pages = normalize_sidebar_visible_pages(existing_runtime.get('sidebar_visible_pages'))
    return success_response({'order': order, 'visible_pages': visible_pages})


@router.post("/api/sidebar-order")
async def save_sidebar_order(req: dict = Body(default={})):
    """保存侧边栏导航顺序和可见页面。"""
    existing_runtime = config_manager.get_runtime_config()
    order = existing_runtime.get('sidebar_order', [])

    if 'order' in req:
        order = normalize_sidebar_order(req.get('order'))
        existing_runtime['sidebar_order'] = order

    if 'visible_pages' in req:
        visible_pages = normalize_sidebar_visible_pages(req.get('visible_pages'))
        if not visible_pages:
            return error_response("侧边栏至少需要保留一个可见页面", status_code=400)
        existing_runtime['sidebar_visible_pages'] = visible_pages

    if 'order' not in req and 'visible_pages' not in req:
        return error_response("缺少可保存的侧边栏配置", status_code=400)

    if config_manager.save_runtime_config(existing_runtime):
        return success_response({
            'order': order if isinstance(order, list) else [],
            'visible_pages': existing_runtime.get('sidebar_visible_pages', []),
        })
    return error_response("保存侧边栏排序失败", status_code=500)


# ==================== 设备分组（per-user）====================

# 设备分组按登录用户隔离：每人一份 data/user_prefs/<username>/device_groups.json。
# 未登录（匿名）时回退到全局 runtime config 的 device_groups，保持旧行为兼容。


def current_username_for_request(request: Request | None) -> str | None:
    """取当前登录用户名；未登录返回 None（走全局匿名回退）。"""
    if request is None:
        return None
    try:
        from features.auth import get_authenticated_user
        user = get_authenticated_user(request)
    except Exception:
        return None
    return getattr(user, 'username', None) if user else None


def _device_groups_dir(username: str) -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'data', 'user_prefs', username)
    os.makedirs(base, exist_ok=True)
    return base


def _device_groups_path(username: str) -> str:
    return os.path.join(_device_groups_dir(username), 'device_groups.json')


def _anonymous_groups_key() -> str:
    return 'device_groups'


def load_device_groups(username: str | None) -> list[dict[str, Any]]:
    """读取并规整指定用户的 device_groups。username 为 None 时走全局（匿名回退）。"""
    if not username:
        if config_manager is None:
            return []
        try:
            raw = config_manager.get_runtime_config().get(_anonymous_groups_key(), [])
        except Exception:
            return []
        return normalize_device_groups(raw)
    path = _device_groups_path(username)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return normalize_device_groups(data.get('groups', []) if isinstance(data, dict) else data)


def save_device_groups(username: str | None, groups: list[dict[str, Any]]) -> bool:
    if not username:
        if config_manager is None:
            return False
        try:
            existing = config_manager.get_runtime_config()
            existing[_anonymous_groups_key()] = groups
            return config_manager.save_runtime_config(existing)
        except Exception:
            return False
    path = _device_groups_path(username)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'groups': groups}, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


@router.get("/api/device-groups")
async def get_device_groups(request: Request):
    """获取当前用户的设备分组定义。"""
    username = current_username_for_request(request)
    return success_response({'groups': load_device_groups(username)})


@router.post("/api/device-groups")
async def mutate_device_groups(request: Request, req: dict = Body(default={})):
    """设备分组增删改 / 重排 / 分配设备（per-user）。

    action:
      create  {name, color?, device_ids?, followed?}      -> 新建分组（id 后端生成）
      update  {id, name?, color?, device_ids?, followed?} -> 更新分组字段
      delete  {id}                                         -> 删除分组（其设备归未分组）
      reorder {ids: [id,...]}                              -> 按给定顺序重排
      assign  {id, device_ids, mode: "set"|"add"|"remove"}-> 设置/追加/移除组内设备
    """
    action = (req.get('action') or '').strip()
    if action not in {'create', 'update', 'delete', 'reorder', 'assign'}:
        return error_response("action 必须是 create/update/delete/reorder/assign", status_code=400)

    username = current_username_for_request(request)
    groups = load_device_groups(username)

    if action == 'create':
        name = str(req.get('name') or '').strip()
        if not name:
            return error_response("分组名称不能为空", status_code=400)
        group_id = _gen_group_id(groups)
        device_ids = _coerce_device_ids(req.get('device_ids'))
        groups.append({
            'id': group_id,
            'name': name,
            'color': str(req.get('color') or '').strip() or _default_group_color(len(groups)),
            'device_ids': device_ids,
            'followed': bool(req.get('followed', False)),
        })
        enforce_exclusive_device_group(groups, group_id, device_ids)

    elif action == 'update':
        group = _find_group(groups, req.get('id'))
        if not group:
            return error_response("分组不存在", status_code=404)
        if 'name' in req:
            name = str(req.get('name') or '').strip()
            if not name:
                return error_response("分组名称不能为空", status_code=400)
            group['name'] = name
        if 'color' in req:
            color = str(req.get('color') or '').strip()
            if color:
                group['color'] = color
        if 'device_ids' in req:
            group['device_ids'] = _coerce_device_ids(req.get('device_ids'))
            enforce_exclusive_device_group(groups, group['id'], group['device_ids'])
        if 'followed' in req:
            group['followed'] = bool(req.get('followed'))

    elif action == 'delete':
        group_id = str(req.get('id') or '').strip()
        groups = [g for g in groups if g['id'] != group_id]

    elif action == 'reorder':
        ids = [str(x).strip() for x in (req.get('ids') or []) if isinstance(x, str)]
        by_id = {g['id']: g for g in groups}
        ordered = [by_id[i] for i in ids if i in by_id]
        # 追加未在 ids 中出现的分组，保持其原相对顺序
        ordered.extend([g for g in groups if g['id'] not in ids])
        groups = ordered

    elif action == 'assign':
        group = _find_group(groups, req.get('id'))
        if not group:
            return error_response("分组不存在", status_code=404)
        mode = (req.get('mode') or 'set').strip()
        incoming = set(_coerce_device_ids(req.get('device_ids')))
        if mode == 'set':
            group['device_ids'] = list(incoming)
            enforce_exclusive_device_group(groups, group['id'], group['device_ids'])
        elif mode == 'add':
            existing = set(group['device_ids']) | incoming
            group['device_ids'] = list(existing)
            enforce_exclusive_device_group(groups, group['id'], list(incoming))
        elif mode == 'remove':
            existing = set(group['device_ids']) - incoming
            group['device_ids'] = list(existing)
        else:
            return error_response("mode 必须是 set/add/remove", status_code=400)

    if not save_device_groups(username, groups):
        return error_response("保存设备分组失败", status_code=500)
    return success_response({'groups': groups})


def _coerce_device_ids(raw: Any) -> list[str]:
    """把任意输入规整成去重的非空设备序列号列表。"""
    if not isinstance(raw, list):
        return []
    seen, result = set(), []
    for dev in raw:
        if not isinstance(dev, str):
            continue
        dev = dev.strip()
        if dev and dev not in seen:
            seen.add(dev)
            result.append(dev)
    return result


def enforce_exclusive_device_group(groups: list[dict[str, Any]], owner_id: str, device_ids: list[str]) -> None:
    """互斥语义：device_ids 这些设备只能属于 owner_id 这一组，从其他组移除。"""
    owned = set(device_ids)
    for g in groups:
        if g['id'] == owner_id:
            continue
        g['device_ids'] = [d for d in g.get('device_ids', []) if d not in owned]


def _find_group(groups: list[dict[str, Any]], group_id: Any) -> dict[str, Any] | None:
    gid = str(group_id or '').strip()
    for g in groups:
        if g['id'] == gid:
            return g
    return None


def _gen_group_id(existing: list[dict[str, Any]]) -> str:
    """生成不与现有 id 冲突的分组 id（g_ 前缀 + 6 位）。"""
    import secrets
    taken = {g['id'] for g in existing}
    while True:
        gid = 'g_' + secrets.token_hex(3)
        if gid not in taken:
            return gid
