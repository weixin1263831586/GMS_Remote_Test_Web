"""Configuration and Tailscale routes - config CRUD, sidebar order, Tailscale status."""

import asyncio
import logging
import os
import re
import subprocess
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from features.auth import (
    CurrentUser,
    require_authenticated_user_when_auth_required,
    require_elevated_admin,
    require_elevated_admin_when_auth_required,
)
from foundation.responses import error_response, success_response
from foundation.static_routes import apply_static_routes

from . import runtime
from .clients import (
    get_client_display_id_from_request,
    get_client_id_from_request,
    hide_sensitive_info,
    owner_id_from_request,
    parse_client_id,
)
from .navigation_preferences import (
    load_navigation_preferences,
    save_navigation_preferences,
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
    if request is not None:
        display_id = get_client_display_id_from_request(request)
        if "@" in display_id:
            return display_id
    runtime_config = config_manager.get_runtime_config()
    runtime_local_server = str(runtime_config.get("local_server") or "").strip()
    if "@" in runtime_local_server:
        return runtime_local_server
    return client_id


logger = logging.getLogger(__name__)

router = APIRouter()


def _update_runtime_sections(updates: dict[str, Any]) -> bool:
    """Use atomic section updates, retaining compatibility with test doubles."""
    updater = getattr(config_manager, 'update_runtime_config', None)
    if callable(updater):
        return updater(updates)
    existing = config_manager.get_runtime_config()
    existing.update(updates)
    return config_manager.save_runtime_config(existing)


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


# ==================== Routes ====================

@router.get("/api/config/external-services")
async def get_external_services_config(
    _user: CurrentUser | None = Depends(
        require_authenticated_user_when_auth_required
    ),
):
    """Return user-editable external service addresses without exposing secrets."""
    config = config_manager.load_config()
    external = config.get("external_services") or {}
    return success_response({
        "gms_assistant_url": str(external.get("gms_assistant_url") or "").strip().rstrip("/"),
    })


@router.post("/api/config/external-services")
async def update_external_services_config(
    req: dict[str, Any],
    _admin: CurrentUser | None = Depends(
        require_elevated_admin_when_auth_required
    ),
):
    """Save the GMS Assistant upstream address in runtime configuration.

    The assistant app is reached same-origin via the /public, /assets, ...
    proxy routes, which prepend this value as the upstream origin. Storing a
    full page URL (e.g. .../public/agents/<id>/chat) makes the proxy fetch
    <origin>/<page-path>/assets/... and 404, so any path/query/fragment is
    stripped down to the scheme://netloc origin.
    """
    url = str(req.get("gms_assistant_url") or "").strip().rstrip("/")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return error_response("GMS助手地址必须是完整的 http(s) URL", status_code=400)
        url = f"{parsed.scheme}://{parsed.netloc}"
    external = dict(config_manager.load_config().get("external_services") or {})
    external["gms_assistant_url"] = url
    if not _update_runtime_sections({"external_services": external}):
        return error_response("保存 GMS助手配置失败", status_code=500)
    return success_response({"gms_assistant_url": url})

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
    effective_ubuntu_user = config_manager.get_ubuntu_user(config)
    configured_suites_path = str(config.get("suites_path") or "").strip()
    if configured_suites_path == "~":
        effective_suites_path = f"/home/{effective_ubuntu_user}"
    elif configured_suites_path.startswith("~/"):
        effective_suites_path = f"/home/{effective_ubuntu_user}/{configured_suites_path[2:]}"
    else:
        effective_suites_path = configured_suites_path or f"/home/{effective_ubuntu_user}/GMS-Suite"
    # 编辑值保持原样，操作值使用解析后的运行时默认配置。
    safe_config["effective_ubuntu_user"] = effective_ubuntu_user
    safe_config["effective_suites_path"] = effective_suites_path
    wifi = config.get("wifi") if isinstance(config.get("wifi"), dict) else {}
    if isinstance(safe_config.get("wifi"), dict):
        safe_config["wifi"].pop("encrypted_password", None)
        # Wi-Fi password is only shown to elevated admin sessions. Client
        # sessions (role="user") that have been verified by an admin count
        # as elevated; ordinary users still see an empty string.
        from features.auth import is_elevated

        if is_elevated(request):
            defaults = config_manager.get_wifi_defaults(config)
            safe_config["wifi"]["password"] = defaults.get("password", "")
        else:
            safe_config["wifi"]["password"] = ""
        safe_config["wifi"]["has_password"] = bool(
            os.getenv("GMS_WIFI_PASSWORD")
            or wifi.get("encrypted_password")
            or wifi.get("password")
        )
    return JSONResponse(content=safe_config)


@router.get("/api/config/opengrok")
async def get_opengrok_config(request: Request):
    """获取OpenGrok配置 - 供前端源码链接使用"""
    config = config_manager.load_config()
    opengrok_config = config.get('opengrok', {})

    if not opengrok_config or 'base_url' not in opengrok_config:
        return error_response('OpenGrok未配置，请在configs/config.json中配置opengrok段', status_code=404)

    return success_response(opengrok_config)


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
async def ensure_tailscale_url(
    request: Request,
    _admin: CurrentUser = Depends(require_elevated_admin),
):
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
            # Privilege escalation is never performed by the web process.
            # Installation enables tailscaled; interactive account enrollment
            # remains an explicit host-administration action.
            svc_check = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "is-active", "--quiet", "tailscaled"],
                capture_output=True, text=True, timeout=5
            )
            if svc_check.returncode != 0:
                return error_response(
                    'tailscaled 服务未运行；请由主机管理员执行 systemctl enable --now tailscaled',
                    status_code=503,
                )

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
                '再刷新此页面。',
                status_code=503
            )
        except Exception as e:
            return error_response(f'Tailscale 启动失败：{e!s}', status_code=503)


@router.post("/api/config/update")
async def update_config(
    req: dict,
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """更新配置 - 只修改运行时配置，禁止修改config.json"""
    # 运行时配置字段（保存在 configs/config_runtime.json）
    # client_ip 和 client_username 属于运行时状态，不持久化。
    runtime_keys = {'client_hosts', 'local_server'}

    # 检查是否有不允许修改的字段
    invalid_fields = set(req.keys()) - runtime_keys
    if invalid_fields:
        return error_response(
            f"不允许修改以下字段: {', '.join(invalid_fields)}. 可修改的字段: {', '.join(runtime_keys)}",
            status_code=400,
        )

    # 只更新允许字段，保留 Gerrit/Redmine/USB-IP 等其他运行时段落。
    runtime_updates = {key: req[key] for key in runtime_keys if key in req}

    # 保存运行时配置
    if _update_runtime_sections(runtime_updates):
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
            "has_password": bool(
                cred.get("encrypted_password") or cred.get("password")
            ),
        }
        public.append(item)
    return public


def _validate_ssh_device_host(device_host: str) -> bool:
    return bool(_SSH_DEVICE_HOST_RE.fullmatch(device_host or ""))


@router.get("/api/config/client-ssh-credentials")
async def list_client_ssh_credentials(
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """列出已保存的客户端 SSH 凭据（密码脱敏）。"""
    runtime_cfg = config_manager.get_runtime_config()
    credentials = runtime_cfg.get("client_ssh_credentials") or []
    if not isinstance(credentials, list):
        credentials = []
    return success_response({"credentials": _public_credentials(credentials)})


@router.post("/api/config/client-ssh-credentials")
async def upsert_client_ssh_credential(
    req: dict = Body(default={}),
    _admin: CurrentUser = Depends(require_elevated_admin),
):
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
async def delete_client_ssh_credential(
    req: dict = Body(default={}),
    _admin: CurrentUser = Depends(require_elevated_admin),
):
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


# ==================== Static Routes ====================

def _load_static_routes_config() -> dict:
    """读取合并后的 static_routes 配置（config.json 默认值 + 运行时覆盖）。"""
    config = config_manager.load_config()
    routes_config = config.get('static_routes')
    if not isinstance(routes_config, dict):
        routes_config = {}
    routes = routes_config.get('routes')
    if not isinstance(routes, list):
        routes = []
    return {
        'enabled': bool(routes_config.get('enabled', False)),
        'routes': [entry for entry in routes if isinstance(entry, dict)],
    }


def _public_route(entry: dict) -> dict:
    return {
        'destination': str(entry.get('destination') or '').strip(),
        'gateway': str(entry.get('gateway') or '').strip(),
    }


def _validate_static_routes_payload(routes: Any, enabled: Any) -> tuple[list[dict], bool, str | None]:
    """校验前端提交的路由列表；返回 (规范化列表, enabled, 错误信息)。"""
    if not isinstance(routes, list):
        return [], False, 'routes 必须是数组'
    normalized = []
    seen = set()
    for entry in routes:
        if not isinstance(entry, dict):
            return [], False, '路由条目格式错误'
        route = _public_route(entry)
        try:
            import ipaddress

            ipaddress.ip_network(route['destination'], strict=False)
        except ValueError:
            return [], False, f"无效的目标网段: {route['destination'] or '(空)'}"
        try:
            import ipaddress

            ipaddress.ip_address(route['gateway'])
        except ValueError:
            return [], False, f"无效的网关地址: {route['gateway'] or '(空)'}"
        key = (route['destination'], route['gateway'])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(route)
    return normalized, bool(enabled), None


@router.get("/api/config/static-routes")
async def get_static_routes_config(
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """读取静态路由配置（系统配置弹框使用）。"""
    config = _load_static_routes_config()
    return success_response(config)


@router.post("/api/config/static-routes")
async def update_static_routes_config(
    req: dict[str, Any],
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """保存静态路由配置到运行时配置，并立即应用到本机路由表。

    保存即生效：成功添加的路由立即进入内核路由表；单条失败不阻塞
    其他路由，失败详情在返回的 results 中逐条给出。
    """
    enabled = req.get('enabled')
    routes = req.get('routes')
    if enabled is None and routes is None:
        return error_response("缺少可保存的路由配置", status_code=400)

    current = _load_static_routes_config()
    next_enabled = bool(enabled) if enabled is not None else current['enabled']
    if routes is None:
        routes = current['routes']
    else:
        routes, next_enabled, error = _validate_static_routes_payload(routes, next_enabled)
        if error:
            return error_response(error, status_code=400)

    payload = {'enabled': next_enabled, 'routes': routes}
    if not _update_runtime_sections({'static_routes': payload}):
        return error_response("保存路由配置失败", status_code=500)
    config_manager.invalidate_cache()

    applied = apply_static_routes({'static_routes': payload})
    failed = [item for item in applied if item.get('status') == 'failed']
    return success_response({
        'static_routes': payload,
        'applied': applied,
        'success': not failed,
        'error': (
            '部分路由添加失败（需要 root/sudoers 免密权限）: '
            + '; '.join(f"{item['destination']}: {item.get('error')}" for item in failed)
            if failed else ''
        ),
    })


@router.get("/api/sidebar-order")
async def get_sidebar_order(request: Request):
    """获取侧边栏导航顺序。"""
    owner_id = owner_id_from_request(request)
    preferences = load_navigation_preferences(owner_id)
    order = preferences["order"]
    order = [page for page in order if isinstance(page, str) and page in SIDEBAR_PAGES]
    visible_pages = normalize_sidebar_visible_pages(preferences["visible_pages"])
    return success_response({'order': order, 'visible_pages': visible_pages})


@router.post("/api/sidebar-order")
async def save_sidebar_order(
    request: Request,
    req: dict = Body(default={}),
):
    """保存侧边栏导航顺序和可见页面。"""
    owner_id = owner_id_from_request(request)
    existing = load_navigation_preferences(owner_id)
    order = existing["order"]
    updates = {}

    if 'order' in req:
        order = normalize_sidebar_order(req.get('order'))
        updates['order'] = order

    if 'visible_pages' in req:
        visible_pages = normalize_sidebar_visible_pages(req.get('visible_pages'))
        if not visible_pages:
            return error_response("侧边栏至少需要保留一个可见页面", status_code=400)
        updates['visible_pages'] = visible_pages

    if 'order' not in req and 'visible_pages' not in req:
        return error_response("缺少可保存的侧边栏配置", status_code=400)

    saved = save_navigation_preferences(owner_id, updates)
    return success_response(saved)
