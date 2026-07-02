"""终端会话管理 - PTY通道、SSH终端、ADB Shell"""

import asyncio
import fcntl
import logging
import os
import pty
import select
import shlex
import signal
import struct
import termios
import threading
import time
from typing import Any

import paramiko
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from features.devices.locks import device_lock_manager
from features.system.ssh import ssh_manager
from features.system.state import global_state
from foundation.common_utils import CommonUtils
from foundation.config import config_manager


logger = logging.getLogger(__name__)


class LocalPtyChannel:
    """Minimal Paramiko-like PTY channel for local terminal sessions."""

    def __init__(self, command: list[str], cwd: str | None = None, env: dict[str, str] | None = None):
        self.command = command
        self.cwd = cwd or os.path.expanduser("~")
        self.env = env or os.environ.copy()
        self.pid, self.fd = pty.fork()
        self.closed = False
        self._reaped = False

        if self.pid == 0:
            try:
                os.chdir(self.cwd)
                os.execvpe(command[0], command, self.env)
            except Exception as e:
                os.write(2, f"Failed to start local terminal: {e}\n".encode("utf-8", errors="ignore"))
                os._exit(127)

        os.set_blocking(self.fd, False)

    def recv_ready(self) -> bool:
        if self.closed:
            return False
        readable, _, _ = select.select([self.fd], [], [], 0)
        return bool(readable)

    def recv(self, size: int) -> bytes:
        if self.closed:
            return b""
        try:
            return os.read(self.fd, size)
        except BlockingIOError:
            return b""
        except OSError:
            self.closed = True
            return b""

    def send(self, data: str | bytes) -> int:
        if self.closed:
            return 0
        if isinstance(data, str):
            data = data.encode("utf-8", errors="ignore")
        return os.write(self.fd, data)

    def resize_pty(self, width: int = 120, height: int = 30):
        if self.closed:
            return
        packed = struct.pack("HHHH", height, width, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, packed)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass

        self._terminate_child()

    def _terminate_child(self) -> None:
        """Terminate and reap the PTY child so closed terminals do not become zombies."""
        if self._reaped:
            return

        for sig, grace_seconds in (
            (signal.SIGHUP, 0.2),
            (signal.SIGTERM, 0.5),
            (signal.SIGKILL, 0.0),
        ):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                self._reap_child(block=False)
                return
            except OSError:
                self._reap_child(block=False)
                return

            deadline = time.monotonic() + grace_seconds
            while True:
                if self._reap_child(block=False):
                    return
                if grace_seconds <= 0 or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)

        self._reap_child(block=True)

    def _reap_child(self, *, block: bool) -> bool:
        try:
            waited_pid, _status = os.waitpid(self.pid, 0 if block else os.WNOHANG)
        except ChildProcessError:
            self._reaped = True
            return True
        except OSError:
            return False

        if waited_pid == self.pid:
            self._reaped = True
            return True
        return False


def close_terminal_session_resources(session_info: dict[str, Any]):
    mode = session_info.get('mode')
    channel = session_info.get('channel')
    ssh = session_info.get('ssh')

    try:
        if channel and mode in {'local', 'local_adb', 'adb'}:
            channel.close()
    except Exception:
        pass

    try:
        if mode == 'adb' and ssh:
            ssh_manager.return_connection(ssh)
        elif ssh:
            ssh.close()
    except Exception:
        pass


def create_local_terminal_channel(command: list[str] | None = None) -> LocalPtyChannel:
    shell = os.environ.get("SHELL") or "/bin/bash"
    terminal_command = command or [shell, "-l"]
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    return LocalPtyChannel(terminal_command, cwd=os.path.expanduser("~"), env=env)


async def handle_adb_shell_connect(client_id: str, websocket: WebSocket, serial_no: str, config: dict):
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
        session_id = client_id

        with global_state.terminal_lock:
            if session_id in global_state.terminal_ssh_sessions:
                try:
                    close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,
                'channel': channel,
                'host': config_manager.get_ubuntu_host(config),
                'user': config_manager.get_ubuntu_user(config),
                'mode': backend_mode,
                'serial_no': serial_no,
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop
            }

        logger.info(f"[TERMINAL] ADB Shell session created for device {serial_no}")
        await websocket.send_json({
            'type': 'terminal_connected',
            'mode': 'adb',
            'serial_no': serial_no
        })

        def read_adb_shell_output():
            """后台线程持续读取终端输出"""
            try:
                while True:
                    if session_id not in global_state.terminal_ssh_sessions:
                        break

                    try:
                        current_channel = global_state.terminal_ssh_sessions[session_id]['channel']

                        if current_channel.recv_ready():
                            data_chunk = current_channel.recv(4096)
                            if not data_chunk:
                                break

                            text = data_chunk.decode('utf-8', errors='replace')

                            try:
                                future = asyncio.run_coroutine_threadsafe(
                                    websocket.send_json({
                                        'type': 'terminal_data',
                                        'data': text
                                    }),
                                    loop
                                )
                                future.result(timeout=5)
                            except Exception as e:
                                logger.error(f"[TERMINAL] Error sending ADB data: {e}")
                                break
                        else:
                            time.sleep(0.01)

                    except TimeoutError:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] ADB read error: {e}")
                        break

            except Exception as e:
                logger.error(f"[TERMINAL] ADB read thread error: {e}")
            finally:
                logger.info(f"[TERMINAL] ADB read thread exiting for {session_id}")

        thread = threading.Thread(target=read_adb_shell_output, daemon=True)
        thread.start()

    except Exception as e:
        logger.error(f"[TERMINAL] ADB Shell connection error: {e}")
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'ADB Shell连接失败: {e!s}'
        })


async def handle_terminal_connect(client_id: str, websocket: WebSocket, data: dict):
    try:
        config = config_manager.load_config()
        host = data.get('host', config_manager.get_ubuntu_host(config))
        user = data.get('user', config_manager.get_ubuntu_user(config))
        password = data.get('password', config.get('ubuntu_pswd', ''))
        mode = data.get('mode', 'ssh')
        serial_no = data.get('serial_no', '')

        session_id = client_id

        if mode == 'adb':
            await handle_adb_shell_connect(client_id, websocket, serial_no, config)
            return

        logger.info(f"[TERMINAL] SSH Connection request from {session_id} to {user}@{host}")

        if CommonUtils.is_local_host(host):
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
                    'connected_at': time.time(),
                    'websocket': websocket,
                    'event_loop': loop
                }

            await websocket.send_json({'type': 'terminal_connected', 'mode': 'local'})

            def read_local_terminal_output():
                try:
                    while True:
                        if session_id not in global_state.terminal_ssh_sessions:
                            break
                        try:
                            current_channel = global_state.terminal_ssh_sessions[session_id]['channel']
                            if current_channel.recv_ready():
                                data_chunk = current_channel.recv(4096)
                                if not data_chunk:
                                    break
                                text = data_chunk.decode('utf-8', errors='ignore')
                                future = asyncio.run_coroutine_threadsafe(
                                    websocket.send_json({'type': 'terminal_data', 'data': text}),
                                    loop
                                )
                                future.result(timeout=5)
                            else:
                                time.sleep(0.01)
                        except OSError:
                            break
                        except Exception as e:
                            logger.error(f"[TERMINAL] Local read error: {e}")
                            break
                finally:
                    with global_state.terminal_lock:
                        if session_id in global_state.terminal_ssh_sessions:
                            close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])
                            del global_state.terminal_ssh_sessions[session_id]

            thread = threading.Thread(target=read_local_terminal_output, daemon=True, name=f"terminal_local_read_{session_id}")
            thread.start()
            return

        # Remote SSH terminal
        ssh_config = {
            'hostname': host,
            'username': user,
            'password': password,
            'timeout': 5,
            'use_key_auth': config.get('use_key_auth', False),
            'private_key_path': config.get('private_key_path', '~/.ssh/id_rsa')
        }

        # create_connection + invoke_shell are blocking paramiko calls (connect
        # timeout up to 5s) — build the channel off the event loop so opening a
        # terminal doesn't stall other websocket clients.
        def _open_terminal_channel():
            conn = ssh_manager.create_connection(ssh_config)
            if not conn:
                return None, None
            ch = conn.invoke_shell(term='xterm-256color')
            ch.setblocking(0)
            ch.resize_pty(width=80, height=24)
            return conn, ch

        ssh, channel = await asyncio.to_thread(_open_terminal_channel)
        if not ssh:
            await websocket.send_json({'type': 'terminal_error', 'error': 'SSH连接失败：请检查用户名、密码或密钥配置'})
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
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop
            }

        await websocket.send_json({'type': 'terminal_connected'})

        def read_ssh_terminal_output():
            try:
                while True:
                    if session_id not in global_state.terminal_ssh_sessions:
                        break
                    try:
                        current_channel = global_state.terminal_ssh_sessions[session_id]['channel']
                        if current_channel.recv_ready():
                            data_chunk = current_channel.recv(4096)
                            if not data_chunk:
                                break
                            text = data_chunk.decode('utf-8', errors='replace')
                            try:
                                future = asyncio.run_coroutine_threadsafe(
                                    websocket.send_json({'type': 'terminal_data', 'data': text}),
                                    loop
                                )
                                future.result(timeout=5)
                            except Exception as e:
                                logger.error(f"[TERMINAL] Error sending data: {e}")
                                break
                        else:
                            time.sleep(0.01)
                    except TimeoutError:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] Read error: {e}")
                        break
            except Exception as e:
                logger.error(f"[TERMINAL] Read thread error: {e}")
            finally:
                with global_state.terminal_lock:
                    if session_id in global_state.terminal_ssh_sessions:
                        close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])
                        del global_state.terminal_ssh_sessions[session_id]
                try:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({'type': 'terminal_error', 'error': '连接已断开'}),
                        loop
                    )
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

        thread = threading.Thread(target=read_ssh_terminal_output, daemon=True, name=f"terminal_read_{session_id}")
        thread.start()

    except paramiko.AuthenticationException:
        await websocket.send_json({'type': 'terminal_error', 'error': 'SSH认证失败：用户名或密码错误'})
    except paramiko.SSHException as e:
        await websocket.send_json({'type': 'terminal_error', 'error': f'SSH连接错误：{e!s}'})
    except Exception as e:
        logger.error(f"[TERMINAL] Connection error: {e}")
        await websocket.send_json({'type': 'terminal_error', 'error': f'连接失败：{e!s}'})


async def handle_terminal_input(client_id: str, websocket: WebSocket, data: dict):
    session_id = client_id
    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                input_data = data.get('input', data.get('data', ''))
                global_state.terminal_ssh_sessions[session_id]['channel'].send(input_data)
            except Exception as e:
                logger.error(f"[TERMINAL] Input error for {session_id}: {e}")
                await websocket.send_json({'type': 'terminal_error', 'error': f'发送数据失败：{e!s}'})


async def handle_terminal_resize(client_id: str, websocket: WebSocket, data: dict):
    session_id = client_id
    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                cols = data.get('cols', 120)
                rows = data.get('rows', 30)
                global_state.terminal_ssh_sessions[session_id]['channel'].resize_pty(width=cols, height=rows)
            except Exception as e:
                logger.error(f"[TERMINAL] Resize error for session {session_id}: {e}")


async def refresh_devices_websocket(client_id: str, websocket: WebSocket):
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)

        if ssh:
            try:
                stdout, _stderr, code = ssh_manager.execute_command(ssh, "adb devices", timeout=5)
                if code == 0:
                    lines = stdout.strip().split('\n')[1:]
                    devices_info = []
                    for line in lines:
                        if line.strip():
                            parts = line.split('\t')
                            if len(parts) >= 2:
                                device_id = parts[0]
                                status = parts[1]
                                device_data = {'id': device_id, 'status': status}

                                lock_status = device_lock_manager.get_lock_status(device_id)
                                if lock_status:
                                    device_data['locked'] = True
                                    device_data['locked_by'] = lock_status['locked_by']

                                devices_info.append(device_data)

                    await websocket.send_json({'type': 'devices_updated', 'devices': devices_info})
            except Exception as e:
                logger.error(f"Error refreshing devices: {e}")
            finally:
                ssh_manager.return_connection(ssh)
    except Exception as e:
        logger.error(f"Error in refresh_devices_websocket: {e}")
        await websocket.send_json({'type': 'error', 'message': str(e)})


async def handle_tradefed_list_results(client_id: str, websocket: WebSocket, data: dict):
    from features.test_execution import execute_tradefed_command, parse_tradefed_list_results

    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)

        if not ssh:
            await websocket.send_json({'type': 'tradefed_list_results_error', 'error': 'SSH 连接失败'})
            return

        suite_path = data.get('suite_path', '')
        tradefed_bin = data.get('tradefed_bin', '')

        if not suite_path or not tradefed_bin:
            await websocket.send_json({'type': 'tradefed_list_results_error', 'error': '缺少参数：suite_path 或 tradefed_bin'})
            ssh_manager.return_connection(ssh)
            return

        output, error, code = execute_tradefed_command(ssh, suite_path, tradefed_bin)
        ssh_manager.return_connection(ssh)

        if code == 0:
            parsed = parse_tradefed_list_results(output)
            await websocket.send_json({
                'type': 'tradefed_list_results',
                'success': True,
                'output': output,
                'columns': parsed.get('columns', []),
                'results': parsed.get('results', []),
                'count': len(parsed.get('results', [])),
                'command': f"cd '{suite_path}' && {tradefed_bin} list results"
            })
        else:
            await websocket.send_json({
                'type': 'tradefed_list_results_error',
                'success': False,
                'error': error or f'命令执行失败，退出代码：{code}',
                'command': f"cd '{suite_path}' && {tradefed_bin} list results"
            })

    except Exception as e:
        logger.error(f"[TRADEFED_LIST_RESULTS] Error: {e}")
        await websocket.send_json({'type': 'tradefed_list_results_error', 'success': False, 'error': str(e)})
