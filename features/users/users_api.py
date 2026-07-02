"""User management routes - client info, detection, username, user list."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from features.auth import get_authenticated_user, require_elevated_admin
from foundation.errors import handle_api_errors
from foundation.responses import error_response

from . import runtime
from .clients import (
    get_client_display_id_from_request,
    get_client_id_from_request,
    get_client_ip,
    get_client_source,
    get_client_username_from_request,
    is_manual_username_fallback_error,
    parse_client_id,
)
from .models import ClientInfoRequest
from .sessions import client_manager


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/users/current")
async def get_client_info(request: Request):
    """获取当前平台用户信息（返回 user_id 用于 WebSocket 连接）"""
    client_id = get_client_id_from_request(request)
    user = get_authenticated_user(request)

    runtime.get_or_create_user_state(client_id)

    client_ip = get_client_ip(request)
    username = get_client_username_from_request(request, getattr(user, "username", client_id))
    display_client_id = get_client_display_id_from_request(request)
    with runtime.global_state.user_states_lock:
        state = runtime.global_state.user_states.get(client_id)
        if state is not None:
            state["client_username"] = username
            state["client_ip"] = client_ip
            state["display_client_id"] = display_client_id

    logger.info(f"[ClientInfo] GET - IP: {client_ip} | Username: {username} | ClientID: {client_id}")

    return JSONResponse(content={
        "ip": client_ip,
        "client_id": client_id,
        "display_client_id": display_client_id,
        "username": username,
        "user": user.as_dict() if user else None,
    })


@router.post("/api/users/detect")
async def detect_client(req: ClientInfoRequest, request: Request):
    """自动检测客户端用户名"""
    client_ip = get_client_ip(request, req.ip)

    success, username, error = client_manager.detect_username(
        client_ip,
        req.username,
        req.password
    )

    if success:
        return JSONResponse(content={
            "success": True,
            "username": username
        })
    else:
        manual_allowed = (
            not req.username
            or not req.password
            or is_manual_username_fallback_error(error)
        )
        return JSONResponse(content={
            "success": False,
            "error": error,
            "manual_allowed": manual_allowed
        }, status_code=200 if manual_allowed else 401)


@router.post("/api/users/set-username")
async def set_client_username(req: ClientInfoRequest, request: Request):
    """手动设置客户端用户名（不需要SSH密码）"""
    client_ip = get_client_ip(request, req.ip)
    username = req.username

    if not username or username == 'unknown':
        return error_response("用户名不能为空或unknown", 400)

    # 加载现有动态配置
    existing_runtime = runtime.config_manager.get_runtime_config()
    client_hosts = existing_runtime.get('client_hosts', {})
    client_hosts[client_ip] = username

    # 只保存客户端相关配置
    runtime_config = runtime.config_manager.prepare_client_config({'client_hosts': client_hosts})

    # 保存到配置文件
    if runtime.config_manager.save_runtime_config(runtime_config):
        # 更新内存中的映射
        client_manager.client_hosts = client_hosts

        display_client_id = f"{username}@{client_ip}" if client_ip and client_ip != "unknown" else username
        client_id = get_client_id_from_request(request)
        with runtime.global_state.user_states_lock:
            state = runtime.global_state.user_states.get(client_id)
            if state is not None:
                state['client_username'] = username
                state['client_ip'] = client_ip
                state['display_client_id'] = display_client_id

        logger.info(f"[Set Username] {client_ip} -> {username}")

        return JSONResponse(content={
            "success": True,
            "username": username,
            "ip": client_ip,
            "client_id": client_id,
            "display_client_id": display_client_id,
        })
    else:
        return error_response("保存配置失败", 500)


@router.delete("/api/users/remove")
@handle_api_errors
async def remove_configured_user(request: Request, _elevated=Depends(require_elevated_admin)):
    """从配置的 client_hosts 中移除一个用户（按 IP）。

    仅能移除「配置型」用户（来自 config_runtime.client_hosts）。临时会话用户
    （仅活跃、未配置）无配置条目，不在此处理范围。正在测试中的用户禁止移除。
    需要管理员提权（近期二次认证）。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip") or "").strip()
    if not ip:
        return error_response("ip is required", 400)

    existing_runtime = runtime.config_manager.get_runtime_config()
    client_hosts = dict(existing_runtime.get("client_hosts") or {})
    if ip not in client_hosts:
        return error_response(f"用户 {ip} 不在配置列表中（可能只是临时会话，无需移除）", 404)

    # 正在测试中的用户禁止移除，避免删配置的同时其测试设备锁残留。
    with runtime.global_state.user_states_lock:
        for state in runtime.global_state.user_states.values():
            if state.get("client_ip") == ip and state.get("running", False):
                return error_response(f"用户 {ip} 正在测试中，请等待其测试结束后再移除", 409)

    del client_hosts[ip]
    runtime_config = runtime.config_manager.prepare_client_config({"client_hosts": client_hosts})
    if not runtime.config_manager.save_runtime_config(runtime_config):
        return error_response("保存配置失败", 500)

    client_manager.client_hosts = client_hosts
    logger.info(f"[Remove User] removed configured user {ip}")
    return JSONResponse(content={"success": True, "ip": ip})


@router.get("/api/users/list")
@handle_api_errors
async def list_users():
    """获取所有在线用户列表"""
    now = datetime.now()
    config = runtime.config_manager.load_config()
    configured_client_hosts = config.get('client_hosts') or {}

    # 本地地址列表，不显示在用户列表中
    local_addresses = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    vpn_gateway_addresses = set(config.get('vpn_gateways', []))

    with runtime.global_state.user_states_lock:
        temp_users = {}
        for ip, username in configured_client_hosts.items():
            ip = str(ip or '').strip()
            username = str(username or '').strip() or 'unknown'
            if not ip or ip in local_addresses or ip in vpn_gateway_addresses:
                continue
            display_client_id = (
                f"{username}@{ip}" if username and username != 'unknown' else ip
            )
            temp_users[f"configured:{display_client_id}"] = {
                'client_id': display_client_id,
                'user_id': '',
                'username': username,
                'ip': ip,
                **get_client_source(ip),
                'running': False,
                'devices': [],
                'last_seen': '',
                'created_at': '',
                'configured': True,
            }

        for client_id, state in runtime.global_state.user_states.items():
            # 检查会话是否活跃（最近24小时内有活动）
            if 'last_seen' in state:
                try:
                    last_seen = datetime.fromisoformat(state['last_seen'])
                    if (now - last_seen) > timedelta(hours=24):
                        continue
                except (ValueError, TypeError):
                    continue

            username_from_id, ip_from_id = parse_client_id(client_id)

            # 优先使用state中存储的username（更准确）
            username = state.get('client_username', username_from_id)
            if username == 'unknown':
                username = username_from_id
            ip = state.get('client_ip') or ip_from_id
            display_client_id = state.get('display_client_id') or (
                f"{username}@{ip}" if username and username != 'unknown' and ip and ip != 'unknown' else client_id
            )

            # 过滤本地地址和VPN网关地址
            if ip in local_addresses or ip in vpn_gateway_addresses:
                continue

            configured_username = str(configured_client_hosts.get(ip) or '').strip()
            is_configured = bool(configured_username)
            if is_configured and (
                not username
                or username == 'unknown'
                or (configured_username and username != configured_username)
            ):
                username = configured_username
                display_client_id = f"{username}@{ip}" if ip and ip != 'unknown' else username

            user_info = {
                'client_id': display_client_id,
                'user_id': client_id,
                'username': username,
                'ip': ip,
                **get_client_source(ip),
                'running': state.get('running', False),
                'devices': state.get('devices', []),
                'last_seen': state.get('last_seen', ''),
                'created_at': state.get('created_at', ''),
                'configured': is_configured,
            }

            # 平台登录用户是状态隔离边界。多用户可能经同一个反向代理或
            # Tailscale 出口访问，不能按 IP 折叠，否则谁刷新就只剩谁。
            user_key = f"host:{display_client_id}" if display_client_id else (client_id or ip)
            temp_users.pop(f"configured:{display_client_id}", None)
            if is_configured and configured_username:
                temp_users.pop(f"configured:{configured_username}@{ip}", None)
            existing = temp_users.get(user_key)
            if existing is None or (existing['username'] == 'unknown' and username != 'unknown'):
                temp_users[user_key] = user_info

        users = list(temp_users.values())

    return JSONResponse(content={
        'total': len(users),
        'users': users
    })
