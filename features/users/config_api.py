"""Configuration and Tailscale routes - config CRUD, sidebar order, Tailscale status."""

import asyncio
import logging
import os
import subprocess
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from foundation.responses import error_response, success_response

from . import runtime
from .clients import (
    get_client_id_from_request,
    hide_sensitive_info,
)


config_manager = runtime.config_manager


def get_effective_local_server(
    client_id: str,
    requested_local_server: str = "",
) -> str:
    if requested_local_server:
        return requested_local_server
    runtime_config = config_manager.get_runtime_config()
    return runtime_config.get("local_server") or client_id

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


# ==================== Routes ====================

@router.get("/api/config/read")
async def get_config(request: Request):
    """获取配置 - 隐藏敏感信息后返回配置对象"""
    # 跟踪用户访问
    client_id = get_client_id_from_request(request)
    runtime.get_or_create_user_state(client_id)

    config = config_manager.load_config()
    # local_server 是客户端回传地址；没有显式动态配置时，按当前请求用户/IP 展示。
    config['local_server'] = get_effective_local_server(client_id)

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

    # 合并现有配置和请求配置（单次遍历）
    runtime_updates = {
        k: req.get(k, existing_runtime.get(k))
        for k in runtime_keys
        if k in existing_runtime or k in req
    }

    # 保存运行时配置
    if config_manager.save_runtime_config(runtime_updates):
        return success_response()
    else:
        return error_response("保存配置失败", status_code=500)


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
