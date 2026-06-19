"""Integrations router - SSH, VPN, Redmine, ADB forward, and USB/IP APIs."""

import asyncio
import ipaddress
import logging
import re
import shlex
import subprocess

from fastapi import APIRouter, Body, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from features.devices.support import (
    DeviceSSHConnection,
)
from features.redmine.client import RedmineClient
from features.system.models import VPNConnectRequest
from features.system.network import (
    _extract_network,
    _generate_route_commands,
    _parse_ping_output,
    _validate_ip_address,
    are_same_network,
    check_local_vpn_connected,
    configure_network_dependencies,
    execute_config_host_command,
    get_primary_vpn_target,
    parse_vpn_connection_names,
    resolve_vpn_connection_name,
)
from features.system.ssh import SSHD_INSTALL_GUIDE, ssh_manager
from features.users import get_client_id_from_request, get_client_ip, resolve_tailscale_device_host
from foundation.common_utils import CommonUtils
from foundation.config import config_manager
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)

router = APIRouter()

configure_network_dependencies(
    ssh_manager=ssh_manager,
    is_config_host_local=config_manager.is_config_host_local,
)


@router.get("/api/ssh/sshd")
@handle_api_errors
async def check_ssh_sshd(request: Request, device_host: str | None = Query(None, description="设备主机地址 (user@ip 格式，如 user@192.168.1.100)")):
    """检查SSH服务状态（如未安装则返回安装指南）

    通过SSH连接到Windows客户端检查SSHD服务状态。
    支持查询参数 device_host 来检查指定主机的状态。
    注意：device_host 必须是 user@ip 格式，例如 user@192.168.1.100
    """
    def exec_ssh_cmd(ssh, cmd):
        """执行SSH命令并返回输出"""
        _, stdout, _ = ssh.exec_command(cmd, timeout=10)
        return stdout.read().decode('utf-8', errors='ignore').strip()

    config = config_manager.load_config()
    # 优先使用查询参数中的 device_host，否则使用请求中的客户端ID
    if not device_host:
        client_id = get_client_id_from_request(request)
        tunnel_host, _ = resolve_tailscale_device_host(request, client_id)
        # Tailscale 直连模式：通过 SSH 检查 Windows SSHD 状态
        # 直接走下面的正常 SSH 检查路径，device_host 为 Tailscale IP
        device_host = tunnel_host or client_id

    # 验证 device_host 格式
    if device_host and '@' not in device_host:
        return JSONResponse(content={
            'success': False,
            'error': f'设备主机格式错误："{device_host}"。正确格式应为 user@ip，例如 user@192.168.1.100',
            'installed': False,
            'running': False
        }, status_code=400)

    config['device_host'] = device_host
    found_pwd = config_manager.find_device_host_password(device_host, config)
    config['device_pswd'] = found_pwd or config.get('device_pswd', '')

    # 提前检查：如果没密码则明确提示，而非让连接失败后报通用错误
    if not config.get('device_pswd'):
        return JSONResponse(content={
            'success': False,
            'installed': False,
            'running': False,
            'error': f'未找到 {device_host} 的 SSH 密码，请先在客户端管理中添加该主机的凭据'
        })

    try:
        with DeviceSSHConnection(config) as ssh:
            # 检查是否已安装（先找文件，再查服务）
            installed = bool(exec_ssh_cmd(ssh, "where sshd.exe 2>nul"))
            if not installed:
                installed = bool(exec_ssh_cmd(ssh, "sc query sshd 2>nul | findstr /C:\"RUNNING\" /C:\"STOPPED\""))

            # 检查是否运行中
            running = bool(exec_ssh_cmd(ssh, "sc query sshd | findstr /C:\"RUNNING\" 2>nul"))

            logger.info(f"[SSHD Check] {device_host}: installed={installed}, running={running}")

            return JSONResponse(content={
                'success': True,
                'installed': installed,
                'running': running,
                'install_guide': SSHD_INSTALL_GUIDE if not installed else None
            })
    except Exception as _exc:
        import traceback
        logger.warning(f"[SSHD Check] Cannot connect to {device_host}: {traceback.format_exc()}")
        return JSONResponse(content={
            "success": False,
            "installed": False,
            "running": False,
            "install_guide": SSHD_INSTALL_GUIDE,
            "error": f"无法通过 SSH 连接到 {device_host}，请检查网络连接和目标主机状态"
        })


def _get_network_address(ip: str) -> str:
    """Extract the /24 network address for an IP, with fallback."""
    try:
        return str(ipaddress.IPv4Network(f"{ip}/24", strict=False).network_address)
    except (ipaddress.AddressValueError, ValueError):
        return '.'.join(ip.split('.')[:3]) + '.0'


@router.get("/api/ssh/route")
@handle_api_errors
async def check_ssh_route(request: Request):
    """检查网络路由 - 检查测试主机和设备主机是否在同一网段"""
    config = config_manager.load_config()

    ubuntu_host = config.get("ubuntu_host", "")
    client_ip = get_client_ip(request)

    if not ubuntu_host or client_ip == 'unknown':
        return JSONResponse(content={
            'success': False,
            'error': '无法获取主机IP地址'
        }, status_code=400)

    ubuntu_ip = CommonUtils.extract_ip_from_host(ubuntu_host)
    device_ip = CommonUtils.extract_ip_from_host(client_ip)

    same_network = are_same_network(ubuntu_ip, device_ip)
    need_route = not same_network

    # 先测试实际连通性
    connectivity_ok = False
    latency = None
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ['ping', '-c', '1', '-W', '2', ubuntu_ip],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            connectivity_ok = True
            # 提取延迟时间
            match = re.search(r'time=([\d.]+)', result.stdout)
            if match:
                latency = f"{match.group(1)}ms"
    except Exception as e:
        logger.warning(f"Ping test failed: {e}")

    # 只有网段不同且实际不通时才提示添加路由
    if need_route and not connectivity_ok:
        ubuntu_network = _get_network_address(ubuntu_ip)
        device_network = _get_network_address(device_ip)

        route_commands = {
            'windows': [
                f"route add {ubuntu_network} mask 255.255.255.0 {device_ip}",
                f"route add {device_network} mask 255.255.255.0 {ubuntu_ip}",
                "# 检查路由表: route print",
                f"# 删除路由表: route delete {ubuntu_network}",
                f"# 删除路由表: route delete {device_network}"
            ],
            'linux': [
                f"sudo ip route add {ubuntu_network}/24 via {device_ip}",
                f"sudo ip route add {device_network}/24 via {ubuntu_ip}",
                "# 检查路由表: ip route show",
                f"# 删除路由表: sudo ip route del {ubuntu_network}/24",
                f"# 删除路由表: sudo ip route del {device_network}/24"
            ]
        }

        return JSONResponse(content={
            'success': True,
            'same_network': False,
            'need_route': True,
            'connectivity_ok': False,
            'message': f'⚠️ 网段不同且无法连通: {ubuntu_ip} (网段: {ubuntu_network}/24) ↔ {device_ip} (网段: {device_network}/24)',
            'ubuntu_ip': ubuntu_ip,
            'device_ip': device_ip,
            'ubuntu_network': ubuntu_network,
            'device_network': device_network,
            'route_commands': route_commands,
            'warning': '测试主机和设备主机不在同一网段且无法连通，建议添加路由表'
        })
    elif need_route and connectivity_ok:
        # 网段不同但已连通，路由已配置
        ubuntu_network = _get_network_address(ubuntu_ip)

        return JSONResponse(content={
            'success': True,
            'same_network': False,
            'need_route': False,
            'connectivity_ok': True,
            'latency': latency,
            'message': f'✅ 网段不同但已连通: {ubuntu_ip} (延迟: {latency}) ↔ {device_ip}',
            'ubuntu_ip': ubuntu_ip,
            'device_ip': device_ip,
            'network': ubuntu_network,
            'note': '网段不同但路由已配置，网络通信正常'
        })
    else:
        # 同网段
        ubuntu_network = _get_network_address(ubuntu_ip)

        return JSONResponse(content={
            'success': True,
            'same_network': True,
            'need_route': False,
            'connectivity_ok': connectivity_ok,
            'latency': latency,
            'message': f'✅ 网段相同: {ubuntu_ip} ↔ {device_ip}' + (f' (延迟: {latency})' if latency else ''),
            'ubuntu_ip': ubuntu_ip,
            'device_ip': device_ip,
            'network': ubuntu_network
        })


@router.post("/api/ssh/ping")
async def ping_route_test(request: Request):
    """测试测试主机和客户端的网络连通性"""
    try:
        # 获取请求数据
        data = await request.json()
        test_host_ip = data.get('test_host_ip', '').strip()
        client_ip = data.get('client_ip', '').strip()

        # 验证IP格式
        if not _validate_ip_address(test_host_ip) or not _validate_ip_address(client_ip):
            return JSONResponse(
                content={'success': False, 'error': 'IP地址格式不正确'},
                status_code=400
            )

        # 检查是否在同一网段
        test_network = _extract_network(test_host_ip)
        client_network = _extract_network(client_ip)
        same_network = (test_network == client_network)

        # 尝试真正的ping测试（从测试主机ping客户端）
        reachable = False
        latency = None

        if same_network:
            # 同一网段，理论上可达
            reachable = True
            latency = '<1ms (同一网段)'
        else:
            # 不同网段，需要从测试主机执行ping来验证连通性
            try:
                config = config_manager.load_config()
                with ssh_manager.optional_connection(config) as ssh:
                    if ssh:
                        # 从测试主机ping客户端IP
                        ping_cmd = f"ping -c 3 -W 2 {client_ip}"
                        _, stdout, stderr = ssh.exec_command(ping_cmd, timeout=10)

                        # 读取ping输出（限制大小防止内存溢出）
                        ping_output = stdout.read(8192).decode('utf-8', errors='ignore')   # 8KB sufficient for ping
                        stderr.read(2048).decode('utf-8', errors='ignore')   # 2KB sufficient for errors
                        exit_status = stdout.channel.recv_exit_status()

                        # 解析ping结果
                        reachable, latency = _parse_ping_output(ping_output, exit_status)

                        logger.info(f"Ping test from {test_host_ip} to {client_ip}: reachable={reachable}, latency={latency}")

            except Exception as e:
                logger.warning(f"Ping test failed: {e}")
                reachable = False
                latency = 'N/A'

        # 准备路由命令（检查测试主机是否需要添加路由到客户端网段）
        route_commands = None
        test_client_different = (test_network != client_network)

        if test_client_different:
            # 测试主机和客户端不在同一网段，需要添加路由
            route_commands = _generate_route_commands(test_network, client_network, test_host_ip)

        return JSONResponse(content={
            'success': True,
            'reachable': reachable,
            'latency': latency,
            'same_network': same_network,
            'test_host_ip': test_host_ip,
            'client_ip': client_ip,
            'test_network': test_network,
            'client_network': client_network,
            'test_client_different': test_client_different,
            'route_commands': route_commands
        })

    except Exception as e:
        logger.error(f"Error in ping route test: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


@router.get("/api/vpn/connections")
@handle_api_errors
async def get_vpn_connections():
    """获取系统中所有可用的 VPN 连接"""
    config = config_manager.load_config()
    ssh = None
    try:
        if not config_manager.is_config_host_local(config):
            ssh = ssh_manager.get_connection(config)
        if not config_manager.is_config_host_local(config) and not ssh:
            return error_response("SSH连接失败", status_code=500)

        cmd = "nmcli -t -f NAME,TYPE connection show 2>/dev/null"
        output, _, _ = await execute_config_host_command(config, ssh, cmd, timeout=5)
        vpn_names = parse_vpn_connection_names(output)

        if ssh:
            ssh_manager.return_connection(ssh)
        return JSONResponse(content={"success": True, "connections": vpn_names})
    except Exception as e:
        if ssh:
            ssh_manager.return_connection(ssh)
        logger.error(f"Error listing VPN connections: {e}")
        return error_response(str(e), status_code=500)


@router.get("/api/vpn/status")
@handle_api_errors
async def get_vpn_status():
    """获取VPN连接状态（多次ping提高可靠性）"""
    config = config_manager.load_config()
    vpn_target = get_primary_vpn_target(config)

    if config_manager.is_config_host_local(config):
        connected = await asyncio.to_thread(check_local_vpn_connected, vpn_target)
        return JSONResponse(content={
            "success": True,
            "connected": connected,
            "source": "local"
        })

    with ssh_manager.optional_connection(config) as ssh:
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        max_attempts = 2
        for attempt in range(max_attempts):
            output, _, _ = ssh_manager.execute_command(
                ssh,
                f"ping -c 1 -W 1 {vpn_target} 2>&1",  # 减少-W timeout从2到1
                timeout=3  # 减少timeout从5到3
            )

            # 检查ping结果（成功则立即返回）
            if '1 packets transmitted, 1 received' in output or '1 received' in output or 'bytes from' in output:
                logger.info(f"[VPN Status] {vpn_target}: connected (attempt {attempt + 1})")
                return JSONResponse(content={"success": True, "connected": True})

        # 所有尝试都失败，尝试通过nmcli检查VPN连接状态
        try:
            nmcli_output, _, _ = ssh_manager.execute_command(
                ssh,
                "nmcli -t -f NAME,TYPE,STATE connection show --active 2>&1",
                timeout=3  # 减少timeout从5到3
            )

            # 检查是否有VPN类型的活跃连接
            if 'vpn' in nmcli_output.lower() or 'tun' in nmcli_output.lower() or 'tap' in nmcli_output.lower():
                logger.info(f"[VPN Status] VPN detected via nmcli: {nmcli_output.strip()}")
                return JSONResponse(content={"success": True, "connected": True})
        except Exception as e:
            logger.warning(f"[VPN Status] nmcli check failed: {e}")

        # 所有尝试都失败
        logger.info(f"[VPN Status] {vpn_target}: disconnected (0/{max_attempts} successful)")
        return JSONResponse(content={"success": True, "connected": False})


@router.post("/api/vpn/connect")
async def connect_vpn(
    req: VPNConnectRequest | None = Body(default=None)
):
    """连接VPN（使用nmcli），账号密码由 Ubuntu 主机 NetworkManager 管理"""
    try:
        config = config_manager.load_config()
        ssh = None
        if not config_manager.is_config_host_local(config):
            ssh = ssh_manager.get_connection(config)

        if not config_manager.is_config_host_local(config) and not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 优先使用前端指定的 VPN 名称，否则自动发现
            vpn_name = (req.vpn_name if req else None) or await resolve_vpn_connection_name(config, ssh, active_only=False)
            if not vpn_name:
                if ssh:
                    ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={
                        "success": False,
                        "error": "未发现 VPN 连接。请在 Ubuntu 主机 NetworkManager 中配置 VPN 账号。"
                    },
                    status_code=400
                )

            vpn_cmd = f"sudo nmcli connection up {shlex.quote(vpn_name)}"
            output, error, code = await execute_config_host_command(
                config,
                ssh,
                vpn_cmd,
                20
            )

            await asyncio.sleep(2)

            if code == 0:
                is_connected = True
                message = 'VPN 连接成功'
            elif 'already active' in (error or ''):
                is_connected = True
                message = 'VPN 已连接'
            elif 'unknown connection' in (error or ''):
                if ssh:
                    ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={
                        "success": False,
                        "error": f"VPN 连接 {vpn_name} 不存在，请先在 NetworkManager 中配置"
                    },
                    status_code=404
                )
            else:
                is_connected = False
                message = f'VPN 连接失败: {error or output}'

            if ssh:
                ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": is_connected,
                "connected": is_connected,
                "message": message,
                "vpn_connection_name": vpn_name,
                "output": (output[:500] if output else '')
            })
        except Exception:
            if ssh:
                ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error connecting VPN: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@router.post("/api/vpn/disconnect")
async def disconnect_vpn():
    """断开VPN（使用nmcli）"""
    try:
        config = config_manager.load_config()
        ssh = None
        if not config_manager.is_config_host_local(config):
            ssh = ssh_manager.get_connection(config)

        if not config_manager.is_config_host_local(config) and not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            vpn_name = await resolve_vpn_connection_name(config, ssh, active_only=True)
            if not vpn_name:
                if ssh:
                    ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={"success": True, "message": "未发现正在连接的 VPN"},
                    status_code=200
                )

            # 使用nmcli断开VPN
            disconnect_cmd = f"sudo nmcli connection down {shlex.quote(vpn_name)}"
            output, error, code = await execute_config_host_command(
                config,
                ssh,
                disconnect_cmd,
                10
            )

            if ssh:
                ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": code == 0,
                "message": "VPN 已断开" if code == 0 else f"VPN 断开失败: {error or output}",
                "vpn_connection_name": vpn_name
            })
        except Exception:
            if ssh:
                ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error disconnecting VPN: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@router.post("/api/redmine/reply")
async def redmine_reply(request: Request):
    """
    向 Redmine Issue 发送回复（支持附件）

    参数：
        issue_id: Redmine Issue ID
        reply_text: 回复内容
        files: 可选附件列表

    返回：
        success: 是否成功
        message: 成功消息
        error: 错误信息（如果失败）
    """
    try:
        content_type = (request.headers.get("content-type") or "").lower()
        files: list[UploadFile] = []
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            issue_id = str(form.get("issue_id") or "").strip()
            reply_text = str(form.get("reply_text") or "").strip()
            files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
        else:
            body = await request.json()
            issue_id = str(body.get("issue_id") or "").strip()
            reply_text = str(body.get("reply_text") or "").strip()

        if not issue_id:
            return error_response('缺少 issue_id 参数', status_code=400)

        if not reply_text:
            return error_response('缺少 reply_text 参数', status_code=400)

        logger.info(f"[Redmine Reply] 准备发送回复到 Issue #{issue_id}，附件数: {len(files)}")

        stored_creds = config_manager.load_redmine_credentials()
        if not stored_creds:
            return error_response('未配置 Redmine 凭证', status_code=401)

        try:
            redmine_config = config_manager.get_redmine_config()
            base_url = redmine_config['base_url']
        except ValueError as e:
            return error_response(str(e), status_code=404)

        attachment_files = []
        for f in files:
            content = await f.read()
            if not content:
                continue
            file_content_type = f.content_type or 'application/octet-stream'
            filename = f.filename or 'attachment'
            logger.info(f"[Redmine Reply] 上传附件: {filename} ({len(content)} bytes)")
            attachment_files.append({'content': content, 'filename': filename, 'content_type': file_content_type})

        client = RedmineClient(base_url, stored_creds.get('username'), stored_creds.get('password'))
        result = await client.reply_issue(issue_id, reply_text, attachment_files)
        attachment_info = f"，携带 {result.get('attachments', 0)} 个附件" if result.get('attachments') else ''
        logger.info(f"[Redmine Reply] 回复已成功发送到 Issue #{issue_id}{attachment_info}")
        return success_response(result, message=f'回复已发送到 Redmine Issue #{issue_id}{attachment_info}')

    except Exception as e:
        logger.error(f"[Redmine Reply] 发送回复失败：{e}")
        return error_response(
            f'发送失败：{e!s}',
            status_code=500,
        )
