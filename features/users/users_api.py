"""User management routes - client info, detection, username, user list."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from features.auth import (
    CurrentUser,
    get_authenticated_user,
    require_elevated_admin,
    require_elevated_admin_when_auth_required,
)
from foundation.devices_port import host_local_device_inventory
from foundation.errors import handle_api_errors
from foundation.responses import error_response

from . import runtime
from .clients import (
    format_client_display_id,
    get_client_display_id_from_request,
    get_client_id_from_request,
    get_client_ip,
    get_client_source,
    get_client_username_from_request,
    is_manual_username_fallback_error,
    parse_client_id,
    resolve_client_display_id,
)
from .models import ClientInfoRequest
from .sessions import client_manager


logger = logging.getLogger(__name__)

router = APIRouter()
USER_ONLINE_WINDOW = timedelta(minutes=5)


def _is_loopback_device_host(device_host: str) -> bool:
    """本地回环主机（如本机浏览器写入的 hcq@127.0.0.1）不参与直连设备枚举。"""
    host = str(device_host or "").rsplit("@", 1)[-1].strip().strip("[]")
    if host.lower() == "::1":
        return True
    return host.split(":", 1)[0].strip().lower() in {
        "127.0.0.1", "localhost", "::1",
    }


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
    client_ip = get_client_ip(request)

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
    client_ip = get_client_ip(request)
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

        display_client_id = format_client_display_id(username, client_ip)
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
async def list_users(
    _user: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """获取所有在线用户列表"""
    now = datetime.now()
    config = runtime.config_manager.load_config()
    configured_client_hosts = config.get('client_hosts') or {}

    # 本地地址列表，不显示在用户列表中
    local_addresses = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    vpn_gateway_addresses = set(config.get('vpn_gateways', []))

    # user_states 在此循环中只读，锁内仅取快照：遍历体中的
    # resolve_client_display_id 会再次申请 user_states_lock（非重入锁），
    # 锁内遍历会同线程死锁，卡死整个事件循环。
    with runtime.global_state.user_states_lock:
        state_items = list(runtime.global_state.user_states.items())

    temp_users = {}
    for ip, username in configured_client_hosts.items():
        ip = str(ip or '').strip()
        username = str(username or '').strip() or 'unknown'
        if not ip or ip in local_addresses or ip in vpn_gateway_addresses:
            continue
        display_client_id = format_client_display_id(username, ip)
        temp_users[f"configured:{display_client_id}"] = {
            'client_id': display_client_id,
            'user_id': '',
            'username': username,
            'ip': ip,
            **get_client_source(ip),
            'running': False,
            'status': 'offline',
            'devices': [],
            'last_seen': '',
            'created_at': '',
            'configured': True,
        }

    for client_id, state in state_items:
        # 检查会话是否活跃（最近24小时内有活动）
        is_online = False
        if 'last_seen' in state:
            try:
                last_seen = datetime.fromisoformat(state['last_seen'])
                if (now - last_seen) > timedelta(hours=24):
                    continue
                is_online = (now - last_seen) <= USER_ONLINE_WINDOW
            except (ValueError, TypeError):
                continue

        username_from_id, ip_from_id = parse_client_id(client_id)

        # 会话 key 可能是平台账号的内部 user_id（认证后的 API/CLI 会话，
        # 如 gms-rt-* 命令轮询产生的状态）。这类 key 不含 @ip，状态里也
        # 没有 client_username/client_ip，直接展示会把裸 token 当用户名、
        # IP 显示 unknown。这里解析回平台账号的用户管理身份，让它并入
        # 同一个人的用户行，而不是多出一行"陌生用户"。
        if '@' not in str(client_id or ''):
            resolved_display = resolve_client_display_id(
                client_id,
                state.get('display_client_id') or '',
            )
            if resolved_display and '@' in resolved_display:
                username_from_id, ip_from_id = parse_client_id(resolved_display)

        # 优先使用state中存储的username（更准确）
        username = state.get('client_username', username_from_id)
        if username == 'unknown':
            username = username_from_id
        ip = state.get('client_ip') or ip_from_id
        display_client_id = format_client_display_id(
            state.get('display_client_id') or username,
            ip,
        ) or client_id

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
            display_client_id = format_client_display_id(username, ip)

        user_info = {
            'client_id': display_client_id,
            'user_id': client_id,
            'username': username,
            'ip': ip,
            **get_client_source(ip),
            'running': state.get('running', False),
            'status': 'testing' if state.get('running', False) else (
                'online' if is_online else 'offline'
            ),
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
            temp_users.pop(
                f"configured:{format_client_display_id(configured_username, ip)}",
                None,
            )
        existing = temp_users.get(user_key)
        if existing is None or (existing['username'] == 'unknown' and username != 'unknown'):
            temp_users[user_key] = user_info

    users = list(temp_users.values())

    # 合并持久化 Worker 任务的所有者和设备租约。
    try:
        from features.users.cluster_access import get_cluster_service

        cluster_service = get_cluster_service()
        jobs = (
            cluster_service.repository.list_jobs(limit=500)
            if cluster_service is not None
            else []
        )
    except (RuntimeError, AttributeError):
        jobs = []
    active_statuses = {
        "created", "queued", "leasing", "assigned", "dispatching", "running",
        "stopping", "collecting", "worker_lost",
    }
    jobs_by_owner: dict[str, list[dict]] = {}
    for job in jobs:
        owner_id = str(job.get("owner_id") or "").strip()
        if owner_id:
            jobs_by_owner.setdefault(owner_id, []).append(job)
    for owner_id, owner_jobs in jobs_by_owner.items():
        owner_display_id = resolve_client_display_id(owner_id)
        active_jobs = [job for job in owner_jobs if job.get("status") in active_statuses]
        leased_devices = [
            lease.get("device_id")
            for job in active_jobs
            for lease in (job.get("leases") or [])
            if lease.get("status") in {"active", "orphaned"} and lease.get("device_id")
        ]
        user_info = next((item for item in users if owner_id in {
            str(item.get("user_id") or ""), str(item.get("client_id") or ""),
            str(item.get("username") or ""),
        } or owner_display_id == str(item.get("client_id") or "")), None)
        if user_info is None:
            # Historical jobs remain available in reports/cluster history, but
            # an inactive owner with no client record is not a manageable user.
            if not active_jobs:
                continue
            owner_username, owner_ip = parse_client_id(owner_display_id)
            user_info = {
                "client_id": owner_display_id,
                "user_id": owner_id,
                "username": owner_username,
                "ip": "" if owner_ip == "unknown" else owner_ip,
                "source": "cluster",
                "source_label": "集群",
                "running": False,
                "status": "offline",
                "devices": [],
                "last_seen": owner_jobs[0].get("updated_at", ""),
                "created_at": owner_jobs[-1].get("created_at", ""),
                "configured": False,
            }
            users.append(user_info)
        user_info["cluster_running"] = bool(active_jobs)
        user_info["running"] = bool(user_info.get("running") or active_jobs)
        if active_jobs:
            user_info["status"] = "testing"
        user_info["devices"] = list(dict.fromkeys([*(user_info.get("devices") or []), *leased_devices]))
        user_info["worker_ids"] = sorted({
            str(job.get("assigned_worker_id") or "") for job in active_jobs
            if job.get("assigned_worker_id")
        })
        user_info["cluster_jobs"] = [{
            "id": job.get("id", ""),
            "attempt_id": job.get("current_attempt_id", ""),
            "worker_id": job.get("assigned_worker_id", ""),
            "status": job.get("status", ""),
            "suite_key": job.get("suite_key", ""),
        } for job in owner_jobs[:20]]

    for user_info in users:
        if user_info.get("running"):
            user_info["status"] = "testing"
        elif user_info.get("status") not in {"online", "offline"}:
            user_info["status"] = "offline"
        if user_info.get("status") == "testing":
            user_info["removable"] = False
            user_info["removal_reason"] = "用户正在测试中，结束测试后才能移除"
        elif user_info.get("configured"):
            user_info["removable"] = True
            user_info["removal_reason"] = ""
        elif user_info.get("source") == "cluster":
            user_info["removable"] = False
            user_info["removal_reason"] = "集群任务所有者不是客户端配置，不能在此移除"
        else:
            user_info["removable"] = False
            user_info["removal_reason"] = "临时在线会话没有持久配置，断开后会自动清理"

    # 用户主机本地直连设备：物理直连的全部设备（含已通过 USB/IP /
    # ADB Proxy 共享出去的，设备仍接在该主机上）；是否被测试操作
    # 占用由"占用设备"列（devices，来自会话状态与集群租约）表达。
    # 枚举由 devices 特性经 foundation 端口提供（TTL 缓存 + 后台刷新），
    # 未接线或首次枚举未完成时为 None，前端显示 '-'。
    for user_info in users:
        host = str(user_info.get("client_id") or "")
        if "@" not in host or _is_loopback_device_host(host):
            continue
        user_info["local_devices"] = host_local_device_inventory(host)

    return JSONResponse(content={
        'total': len(users),
        'users': users
    })
