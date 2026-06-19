"""User management routes - client info, detection, username, user list."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from foundation.errors import handle_api_errors
from foundation.responses import error_response

from . import runtime
from .clients import (
    get_client_id_from_request,
    get_client_ip,
    get_client_source,
    is_manual_username_fallback_error,
    parse_client_id,
)
from .models import ClientInfoRequest
from .sessions import client_manager


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/users/current")
async def get_client_info(request: Request):
    """获取客户端信息（返回client_id用于WebSocket连接）"""
    # 使用统一的client_id获取逻辑（优先从client_hosts读取）
    client_id = get_client_id_from_request(request)

    # 确保用户状态存在
    runtime.get_or_create_user_state(client_id)

    username, client_ip = parse_client_id(client_id)

    logger.info(f"[ClientInfo] GET - IP: {client_ip} | Username: {username} | ClientID: {client_id}")

    return JSONResponse(content={
        "ip": client_ip,
        "client_id": client_id,
        "username": username
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

        # 同时更新 runtime.global_state.user_states 中的用户名
        old_client_id = f"unknown@{client_ip}"
        new_client_id = f"{username}@{client_ip}"

        with runtime.global_state.user_states_lock:
            # 如果存在 unknown@IP 的记录，更新为新用户名
            if old_client_id in runtime.global_state.user_states:
                old_state = runtime.global_state.user_states.pop(old_client_id)
                old_state['client_username'] = username
                runtime.global_state.user_states[new_client_id] = old_state
            # 或者更新已存在的 client_id 的用户名
            elif client_ip in [parse_client_id(k)[1] for k in runtime.global_state.user_states]:
                for key in list(runtime.global_state.user_states.keys()):
                    if key.endswith(f"@{client_ip}"):
                        runtime.global_state.user_states[key]['client_username'] = username
                        # 如果需要，也可以更新 client_id
                        if key != new_client_id:
                            state = runtime.global_state.user_states.pop(key)
                            runtime.global_state.user_states[new_client_id] = state
                        break

        logger.info(f"[Set Username] {client_ip} -> {username}")

        return JSONResponse(content={
            "success": True,
            "username": username,
            "ip": client_ip,
            "client_id": new_client_id
        })
    else:
        return error_response("保存配置失败", 500)


@router.get("/api/users/list")
@handle_api_errors
async def list_users():
    """获取所有在线用户列表"""
    now = datetime.now()
    config = runtime.config_manager.load_config()

    # 本地地址列表，不显示在用户列表中
    local_addresses = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    vpn_gateway_addresses = set(config.get('vpn_gateways', []))

    with runtime.global_state.user_states_lock:
        temp_users = {}
        for client_id, state in runtime.global_state.user_states.items():
            # 检查会话是否活跃（最近24小时内有活动）
            if 'last_seen' in state:
                try:
                    last_seen = datetime.fromisoformat(state['last_seen'])
                    if (now - last_seen) > timedelta(hours=24):
                        continue
                except (ValueError, TypeError):
                    continue

            username_from_id, ip = parse_client_id(client_id)

            # 优先使用state中存储的username（更准确）
            username = state.get('client_username', username_from_id)
            if username == 'unknown':
                username = username_from_id

            # 过滤本地地址和VPN网关地址
            if ip in local_addresses or ip in vpn_gateway_addresses:
                continue

            user_info = {
                'client_id': client_id,
                'username': username,
                'ip': ip,
                **get_client_source(ip),
                'running': state.get('running', False),
                'devices': state.get('devices', []),
                'last_seen': state.get('last_seen', ''),
                'created_at': state.get('created_at', ''),
            }

            # 如果同一个IP有多个用户记录，优先保留非unknown的用户
            existing = temp_users.get(ip)
            if existing is None or (existing['username'] == 'unknown' and username != 'unknown'):
                temp_users[ip] = user_info

        users = list(temp_users.values())

    return JSONResponse(content={
        'total': len(users),
        'users': users
    })
