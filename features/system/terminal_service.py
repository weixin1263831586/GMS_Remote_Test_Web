"""终端会话管理 - PTY通道、SSH终端、ADB Shell"""
import asyncio
import logging
import os
import re
import shlex
import subprocess
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
    from foundation.cluster_port import get_cluster_service

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
        device_rows = {
            str(item.get("id") or ""): item
            for item in cluster.repository.list_devices(requested_worker)
        }
        composite_id = (
            normalized_serial
            if normalized_serial.startswith(f"{requested_worker}:")
            else f"{requested_worker}:{normalized_serial}"
        )
        inventory_row = device_rows.get(composite_id)
        # 归属校验只信任"库存里在线"的行。行缺失（心跳滞后）或 state 为
        # offline（设备已拔走但快照未刷新，例如同端口换了设备）都必须经
        # 实时探测确认，否则 adb shell 会在会话建立后立刻报 device not
        # found，前端只能显示一个没有上下文的"ADB Shell 启动失败"。
        inventory_online = inventory_row is not None and str(
            inventory_row.get("state") or ""
        ).strip().lower() not in {"", "offline"}
        if (inventory_row is None or not inventory_online) and not (
            _live_adb_device_confirmed(
                config,
                normalized_serial,
                host=host,
                user=user,
                password=password,
            )
        ):
            if inventory_row is None:
                logger.warning(
                    "[TERMINAL] Rejected adb target %s on worker %s; inventory=%s",
                    normalized_serial,
                    requested_worker,
                    sorted(device_rows),
                )
                raise ValueError("设备不属于所选 Worker")
            logger.warning(
                "[TERMINAL] Rejected offline adb device %s on worker %s "
                "(inventory state=%s, live probe miss)",
                normalized_serial,
                requested_worker,
                inventory_row.get("state"),
            )
            raise ValueError(
                f"设备 {normalized_serial} 当前不在线，无法打开 ADB Shell"
            )
        if inventory_row is None:
            # 实时探测命中说明设备确实挂在这台 Worker 上，只是库存还没
            # 随心跳/盘点刷新；回填一行让后续连接走快路径。
            cluster.repository.upsert_seen_device(requested_worker, normalized_serial)
        normalized_serial = composite_id.split(":", 1)[1]

    return requested_worker, host, user, password, normalized_serial


def _live_adb_device_confirmed(
    config: dict,
    serial_no: str,
    *,
    host: str,
    user: str,
    password: str,
) -> bool:
    """Inventory-lag fallback: one live `adb devices` probe on the target host.

    cluster_worker_devices 按心跳/盘点节拍刷新，刚插上的设备在下一拍之前
    不在库存里，归属校验会误拒实际在线的设备。这里对终端通道真正要执行
    adb 的那台主机（本机或所选 Worker 的 SSH 主机）做一次实时探测作为
    回退证据。命令是常量（不含序列号，避免 shell 执行边界上的不可信插
    值），序列号匹配在 Python 侧完成。探测失败按"未确认"处理，维持原
    有拒绝路径。
    """
    snapshot = ""
    try:
        # 与 handle_adb_shell_connect 相同的合并方式，保证探测命中的主机
        # 就是随后真正执行 adb shell 的主机。
        probe_config = dict(config)
        probe_config.update({
            "ubuntu_host": host,
            "ubuntu_user": user,
            "ubuntu_pswd": password,
            "host": host,
            "username": user,
            "password": password,
        })
        if config_manager.is_config_host_local(probe_config):
            from foundation.adb_binary import adb_binary

            completed = subprocess.run(
                [adb_binary(), "devices"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            snapshot = completed.stdout or ""
        else:
            # The live probe must reach the selected Worker, rather than the
            # controller's default SSH target retained in ``config``.
            ssh = ssh_manager.get_connection(probe_config)
            if not ssh:
                return False
            try:
                result = ssh_manager.execute_command(ssh, "adb devices", timeout=8)
                snapshot = result.stdout or ""
            finally:
                ssh_manager.return_connection(ssh)
    except (OSError, subprocess.SubprocessError):
        return False
    return re.search(
        rf"^{re.escape(serial_no)}\s+device\b", snapshot, re.MULTILINE
    ) is not None


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
    # The browser advertises xterm-256color and remote Paramiko channels use
    # the same terminal type. Do not inherit TERM=xterm/dumb from the service
    # manager, otherwise readline may choose capabilities that disagree with
    # the xterm.js client during cursor/history redraws.
    env["TERM"] = "xterm-256color"
    return LocalPtyChannel(terminal_command, cwd=os.path.expanduser("~"), env=env)


_PROMPT_WAIT_TIMEOUT = 3.0
_PROMPT_PATTERN = re.compile(rb"[$#] \r?$")


async def _wait_for_shell_prompt(
    channel,
    *,
    timeout: float = _PROMPT_WAIT_TIMEOUT,
) -> None:
    """Drain channel output until a shell prompt (or timeout) appears.

    登录 shell 初始化（profile、motd、bashrc）需要数百毫秒，期间字节持续
    可读。等待提示符出现即可确认 shell 已准备好执行下一条命令，从而避免
    「命令回显先于提示符到达浏览器」造成的启动误判。超时按尽力而为处理：
    即使没等到也继续发送，让原有流程兜底（远程 SSH 通道的 recv 语义由
    ssh_manager 提供，行为与本机 pty 一致）。
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    buffer = b""

    def _poll_once() -> bool:
        nonlocal buffer
        # 读到提示符后继续把当前突发读完，避免下一轮调用在本轮残留字符
        # 上立刻"命中"提示符而跳过等待（真实通道的 recv_ready 会随即转为
        # False，因此不会无限循环）。
        found = _PROMPT_PATTERN.search(buffer) is not None
        while channel.recv_ready():
            chunk = channel.recv(65536)
            if not chunk:
                return True
            buffer += chunk
            if _PROMPT_PATTERN.search(buffer):
                found = True
        return found

    while loop.time() < deadline:
        if _poll_once():
            return
        await asyncio.sleep(0.02)
    # 超时：记录现场便于排障，但按设计继续。
    logger.info(
        "[TERMINAL] Shell prompt not observed before send (waited %.1fs)",
        timeout,
    )


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
        # 本机会话显式使用与设备轮询/实时探测相同的 adb 二进制：登录
        # shell 的 profile 可能指向另一个 platform-tools 版本，混用客户端
        # 会互相杀掉共享 adb server，令刚建立的 shell 瞬间断开。
        # 绝对路径是常量（环境变量/配置钉死），序列号仍经 shlex.quote，
        # shell 执行边界属性不变。远程 Worker 上保持 `adb` 由其自身环境
        # 解析。前端 shell-main.js 的 `\badb\s+-s` 正则对绝对路径同样
        # 命中（路径末尾的 adb 与 -s 之间存在词边界），启动判定不受影响。
        adb_cmd = "adb"
        if backend_mode == "local_adb":
            from foundation.adb_binary import adb_binary

            adb_cmd = adb_binary()
        # pty 行规程会立即回显输入，而登录 shell 仍在初始化。三条命令无
        # 间隔连发时，回显突发会把 `adb ... shell` 推到宿主提示符**之前**，
        # 浏览器前端按"adb 命令之后出现宿主提示符"判定 adb 已退出，直接报
        # 「ADB Shell 启动失败」并关闭连接（服务端随后只看到 session gone）。
        # 前两条准备命令各自等待宿主提示符，保证最终 adb 命令的回显排在
        # 宿主提示符之后。最后一条 *不能* 再 drain：浏览器需要同时收到
        # `adb -s … shell` 回显和 Android 提示符，才能将会话标记为已就绪。
        # 若在此处等待，会吞掉回显（甚至设备提示符），最终误报启动超时。
        # 探测超时按最坏情况继续（不阻塞握手）。
        await _wait_for_shell_prompt(channel)
        for cmd in ('\n\n\n', 'clear\n'):
            channel.send(cmd)
            await _wait_for_shell_prompt(channel)
        channel.send(f'{adb_cmd} -s {shlex.quote(serial_no)} shell\n')

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
            from foundation.cluster_port import get_cluster_service

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
