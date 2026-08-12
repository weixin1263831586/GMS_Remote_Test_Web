"""终端会话管理 - PTY通道、SSH终端、ADB Shell"""
import asyncio
import logging
import os
import shlex
import time
import uuid
from typing import Any

import paramiko
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from features.system.ssh import ssh_manager
from features.system.state import global_state
from foundation.config import config_manager
from foundation.networking import is_local_host

from .terminal_channels import LocalPtyChannel, close_terminal_session_resources
from .terminal_output import start_terminal_output_pump


logger = logging.getLogger(__name__)
TERMINAL_CLAIM_TTL_SECONDS = 120


def resolve_authorized_terminal_target(
    worker_id: str,
    *,
    mode: str = "ssh",
    serial_no: str = "",
) -> tuple[str, str, str, str, str]:
    """Resolve a terminal target exclusively from server-managed inventory.

    Browser supplied host names, usernames, and passwords are intentionally not
    accepted here. ``worker_id`` selects either the configured local host or a
    registered Worker; credentials remain server-side. The returned tuple is
    ``(worker_id, host, user, password, normalized_serial)``.
    """

    if mode not in {"ssh", "adb"}:
        raise ValueError("不支持的终端模式")

    config = config_manager.load_config()
    from features.cluster import get_cluster_service

    cluster = get_cluster_service()
    requested_worker = str(worker_id or "").strip() or cluster.config.local_worker_id
    if requested_worker == cluster.config.local_worker_id:
        host = config_manager.get_ubuntu_host(config) or "localhost"
        user = config_manager.get_ubuntu_user(config)
        password = str(config.get("ubuntu_pswd") or "")
    else:
        worker = cluster.repository.get_worker(requested_worker)
        if not worker or worker.get("status") not in {"online", "busy", "draining"}:
            raise ValueError("所选 Worker 不在线")
        host = str(worker.get("address") or worker.get("hostname") or "").strip()
        user = str((worker.get("capabilities") or {}).get("ssh_user") or "").strip()
        if not host or not user:
            raise ValueError("Worker 缺少 SSH 连接元数据")
        password = (
            config_manager.find_device_host_password(f"{user}@{host}", config)
            or ""
        )

    normalized_serial = str(serial_no or "").strip()
    if mode == "adb":
        if not normalized_serial:
            raise ValueError("缺少设备序列号")
        device_ids = {
            str(item.get("id") or "")
            for item in cluster.repository.list_devices(requested_worker)
        }
        composite_id = (
            normalized_serial
            if normalized_serial.startswith(f"{requested_worker}:")
            else f"{requested_worker}:{normalized_serial}"
        )
        if composite_id not in device_ids:
            raise ValueError("设备不属于所选 Worker")
        normalized_serial = composite_id.split(":", 1)[1]

    return requested_worker, host, user, password, normalized_serial


def terminal_connection_id(websocket: WebSocket) -> str:
    return str(getattr(websocket.state, "terminal_connection_id", "") or "")


def close_websocket_terminal(websocket: WebSocket) -> None:
    connection_id = terminal_connection_id(websocket)
    session_info = None
    if connection_id:
        with global_state.terminal_lock:
            session_info = global_state.terminal_ssh_sessions.pop(connection_id, None)
    if session_info:
        close_terminal_session_resources(session_info)
    claim_registry = getattr(websocket.state, "terminal_claim_registry", None)
    claim_source_id = str(
        getattr(websocket.state, "terminal_claim_source_id", "") or ""
    )
    if claim_registry is not None and claim_source_id:
        claim_registry.release(claim_source_id, status="released")
    websocket.state.terminal_claim_registry = None
    websocket.state.terminal_claim_source_id = ""
    websocket.state.terminal_claim_id = ""
    websocket.state.terminal_claim_generation = 0
    websocket.state.terminal_connection_id = ""


def _terminal_device_claim_valid(session_info: dict[str, Any]) -> bool:
    registry = session_info.get("claim_registry")
    device_key = str(session_info.get("device_key") or "")
    if registry is None or not device_key:
        return True
    active = registry.active_claim(device_key)
    return bool(
        active
        and active.get("id") == session_info.get("claim_id")
        and int(active.get("generation") or 0)
        == int(session_info.get("claim_generation") or 0)
        and active.get("owner_id") == session_info.get("owner_id")
        and active.get("source_id") == session_info.get("claim_source_id")
    )

def create_local_terminal_channel(command: list[str] | None = None) -> LocalPtyChannel:
    shell = os.environ.get("SHELL") or "/bin/bash"
    terminal_command = command or [shell, "-l"]
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    return LocalPtyChannel(terminal_command, cwd=os.path.expanduser("~"), env=env)


async def handle_adb_shell_connect(
    connection_id: str,
    websocket: WebSocket,
    serial_no: str,
    config: dict,
    *,
    worker_id: str,
    owner_id: str,
    claim: dict[str, Any],
    claim_registry: Any,
    claim_source_id: str,
):
    """处理ADB Shell连接 - 通过SSH执行adb shell命令"""
    try:
        if config_manager.is_config_host_local(config):
            ssh = None
            channel = create_local_terminal_channel()
            backend_mode = 'local_adb'
        else:
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                await websocket.send_json({
                    'type': 'terminal_error',
                    'error': 'SSH连接失败'
                })
                return

            channel = ssh.invoke_shell(term='xterm-256color')
            channel.setblocking(0)
            backend_mode = 'adb'

        channel.resize_pty(width=80, height=24)
        for cmd in ('\n\n\n', 'clear\n', f'adb -s {shlex.quote(serial_no)} shell\n'):
            channel.send(cmd)

        loop = asyncio.get_event_loop()
        session_id = connection_id

        with global_state.terminal_lock:
            if session_id in global_state.terminal_ssh_sessions:
                try:
                    close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,
                'channel': channel,
                'connection_id': session_id,
                'host': config_manager.get_ubuntu_host(config),
                'user': config_manager.get_ubuntu_user(config),
                'mode': backend_mode,
                'worker_id': worker_id,
                'serial_no': serial_no,
                'device_key': claim['device_key'],
                'owner_id': owner_id,
                'claim_registry': claim_registry,
                'claim_source_id': claim_source_id,
                'claim_id': claim['id'],
                'claim_generation': claim['generation'],
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop
            }

        logger.info(f"[TERMINAL] ADB Shell session created for device {serial_no}")
        await websocket.send_json({
            'type': 'terminal_connected',
            'mode': 'adb',
            'serial_no': serial_no,
            'connection_id': connection_id,
            'lease_id': claim['id'],
            'generation': claim['generation'],
        })

        def renew_claim(_session: dict[str, Any]) -> bool:
            return claim_registry.renew(
                claim_source_id,
                TERMINAL_CLAIM_TTL_SECONDS,
                device_keys=[claim['device_key']],
            ) == 1

        start_terminal_output_pump(
            session_id,
            websocket,
            loop,
            thread_name=f"terminal_adb_read_{session_id}",
            validate_session=_terminal_device_claim_valid,
            maintain_session=renew_claim,
        )

    except Exception as e:
        close_websocket_terminal(websocket)
        logger.error(f"[TERMINAL] ADB Shell connection error: {e}")
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'ADB Shell连接失败: {e!s}'
        })


async def handle_terminal_connect(client_id: str, websocket: WebSocket, data: dict):
    try:
        principal = getattr(websocket.state, "current_user", None)
        owner_id = str(getattr(principal, "id", "") or client_id)
        owner_username = str(
            getattr(principal, "username", "") or owner_id
        )
        config = config_manager.load_config()
        mode = str(data.get('mode') or 'ssh').strip().lower()
        serial_no = str(data.get('serial_no') or '').strip()
        worker_id = str(data.get('worker_id') or '').strip()
        try:
            worker_id, host, user, password, serial_no = resolve_authorized_terminal_target(
                worker_id,
                mode=mode,
                serial_no=serial_no,
            )
        except ValueError as exc:
            await websocket.send_json({'type': 'terminal_error', 'error': str(exc)})
            return

        close_websocket_terminal(websocket)
        session_id = uuid.uuid4().hex
        websocket.state.terminal_connection_id = session_id

        if mode == 'adb':
            from features.cluster import get_cluster_service

            claim_registry = get_cluster_service().repository.claims
            device_key = f"{worker_id}:{serial_no}"
            claim_source_id = f"terminal:{session_id}"
            acquired, records = claim_registry.acquire(
                [{
                    'device_key': device_key,
                    'worker_id': worker_id,
                    'serial': serial_no,
                }],
                owner_id=owner_id,
                username=owner_username,
                source_type='terminal',
                source_id=claim_source_id,
                ttl_seconds=TERMINAL_CLAIM_TTL_SECONDS,
                allow_existing_source=False,
            )
            if not acquired:
                conflict = records[0]
                await websocket.send_json({
                    'type': 'terminal_error',
                    'error': '设备正由另一个任务或用户占用',
                    'conflict_source': conflict.get('source_type', ''),
                })
                websocket.state.terminal_connection_id = ''
                return
            claim = records[0]
            websocket.state.terminal_claim_registry = claim_registry
            websocket.state.terminal_claim_source_id = claim_source_id
            websocket.state.terminal_claim_id = claim['id']
            websocket.state.terminal_claim_generation = claim['generation']
            adb_config = dict(config)
            adb_config.update({
                'ubuntu_host': host,
                'ubuntu_user': user,
                'ubuntu_pswd': password,
                'host': host,
                'username': user,
                'password': password,
            })
            await handle_adb_shell_connect(
                session_id,
                websocket,
                serial_no,
                adb_config,
                worker_id=worker_id,
                owner_id=owner_id,
                claim=claim,
                claim_registry=claim_registry,
                claim_source_id=claim_source_id,
            )
            return

        logger.info(f"[TERMINAL] SSH Connection request from {session_id} to {user}@{host}")

        if is_local_host(host):
            channel = create_local_terminal_channel()
            channel.resize_pty(width=80, height=24)
            loop = asyncio.get_event_loop()

            with global_state.terminal_lock:
                if session_id in global_state.terminal_ssh_sessions:
                    close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])

                global_state.terminal_ssh_sessions[session_id] = {
                    'ssh': None,
                    'channel': channel,
                    'host': host,
                    'user': user,
                    'mode': 'local',
                    'worker_id': worker_id,
                    'connection_id': session_id,
                    'connected_at': time.time(),
                    'websocket': websocket,
                    'event_loop': loop
                }

            await websocket.send_json({
                'type': 'terminal_connected',
                'mode': 'local',
                'connection_id': session_id,
            })

            start_terminal_output_pump(
                session_id,
                websocket,
                loop,
                thread_name=f"terminal_local_read_{session_id}",
                encoding_errors="ignore",
            )
            return

        # Remote SSH terminal
        ssh_config = {
            'hostname': host,
            'username': user,
            'password': password,
            'timeout': 5,
            # A host-scoped saved password takes precedence over the global
            # key setting. Otherwise an encrypted default key prevents
            # Paramiko from ever attempting the valid Worker password.
            'use_key_auth': bool(config.get('use_key_auth', False) and not password),
            'private_key_path': config.get('private_key_path', '~/.ssh/id_rsa')
        }

        # Paramiko 建连和打开 Shell 均为阻塞调用，在线程中执行。
        def _open_terminal_channel():
            try:
                conn = ssh_manager.create_connection(ssh_config, raise_on_error=True)
                if not conn:
                    return None, None, None
                ch = conn.invoke_shell(term='xterm-256color')
                ch.setblocking(0)
                ch.resize_pty(width=80, height=24)
                return conn, ch, None
            except Exception as exc:
                return None, None, exc

        ssh, channel, connection_error = await asyncio.to_thread(_open_terminal_channel)
        if not ssh:
            close_websocket_terminal(websocket)
            host_key_error = "known_hosts" in str(connection_error or "").lower()
            payload = {
                'type': 'terminal_error',
                'error': (
                    'SSH 主机密钥尚未信任，请先在主机集群页面登记主机密钥'
                    if host_key_error
                    else f'SSH连接失败：请录入或更新 {user}@{host} 的密码'
                ),
            }
            if not host_key_error:
                payload.update({
                    'credential_required': True,
                    'device_host': f'{user}@{host}',
                })
            await websocket.send_json(payload)
            return

        loop = asyncio.get_event_loop()
        with global_state.terminal_lock:
            if session_id in global_state.terminal_ssh_sessions:
                close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,
                'channel': channel,
                'host': host,
                'user': user,
                'worker_id': worker_id,
                'connection_id': session_id,
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop
            }

        await websocket.send_json({
            'type': 'terminal_connected',
            'mode': 'ssh',
            'connection_id': session_id,
        })

        start_terminal_output_pump(
            session_id,
            websocket,
            loop,
            thread_name=f"terminal_read_{session_id}",
            notify_disconnect=True,
        )

    except paramiko.AuthenticationException:
        close_websocket_terminal(websocket)
        await websocket.send_json({'type': 'terminal_error', 'error': 'SSH认证失败：用户名或密码错误'})
    except paramiko.SSHException as e:
        close_websocket_terminal(websocket)
        await websocket.send_json({'type': 'terminal_error', 'error': f'SSH连接错误：{e!s}'})
    except Exception as e:
        close_websocket_terminal(websocket)
        logger.error(f"[TERMINAL] Connection error: {e}")
        await websocket.send_json({'type': 'terminal_error', 'error': f'连接失败：{e!s}'})


async def handle_terminal_input(client_id: str, websocket: WebSocket, data: dict):
    session_id = terminal_connection_id(websocket)
    supplied_id = str(data.get('connection_id') or '')
    if supplied_id and supplied_id != session_id:
        await websocket.send_json({'type': 'terminal_error', 'error': '终端连接标识无效'})
        return
    claim_revoked = False
    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                session_info = global_state.terminal_ssh_sessions[session_id]
                if not _terminal_device_claim_valid(session_info):
                    close_terminal_session_resources(session_info)
                    del global_state.terminal_ssh_sessions[session_id]
                    claim_revoked = True
                else:
                    input_data = data.get('input', data.get('data', ''))
                    session_info['channel'].send(input_data)
            except Exception as e:
                logger.error(f"[TERMINAL] Input error for {session_id}: {e}")
                await websocket.send_json({'type': 'terminal_error', 'error': f'发送数据失败：{e!s}'})
    if claim_revoked:
        close_websocket_terminal(websocket)
        await websocket.send_json({
            'type': 'terminal_error',
            'error': '设备租约已失效，终端已关闭',
        })


async def handle_terminal_resize(client_id: str, websocket: WebSocket, data: dict):
    session_id = terminal_connection_id(websocket)
    supplied_id = str(data.get('connection_id') or '')
    if supplied_id and supplied_id != session_id:
        await websocket.send_json({'type': 'terminal_error', 'error': '终端连接标识无效'})
        return
    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                cols = data.get('cols', 120)
                rows = data.get('rows', 30)
                global_state.terminal_ssh_sessions[session_id]['channel'].resize_pty(width=cols, height=rows)
            except Exception as e:
                logger.error(f"[TERMINAL] Resize error for session {session_id}: {e}")
