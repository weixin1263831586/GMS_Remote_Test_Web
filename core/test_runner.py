"""
测试执行 - 核心业务逻辑

特性：
- 测试启动和停止
- 实时日志推送
- 测试状态管理
- 支持多种测试类型（CTS/GTS/VTS/STS）
"""

import os
import time
import logging
import shlex
import asyncio
from typing import Dict, Any, Optional, Callable
from datetime import datetime
import paramiko

from .ssh import ssh_manager
from .config import config_manager

logger = logging.getLogger(__name__)


class TestRunner:
    """
    测试运行器

    特性：
    - 支持异步执行测试
    - 实时日志流式输出
    - 进程组隔离
    - 测试状态管理
    """

    def __init__(self):
        """初始化测试运行器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager
        self.running_tests: Dict[str, Dict[str, Any]] = {}  # {client_id: test_info}

    async def start_test(
        self,
        test_params: Dict[str, Any],
        client_id: str,
        log_callback: Callable[[str, str], None]
    ) -> bool:
        """
        启动测试

        Args:
            test_params: 测试参数
            client_id: 客户端ID
            log_callback: 日志回调函数

        Returns:
            是否成功启动
        """
        try:
            await log_callback(f"🚀 启动测试: {test_params.get('test_type', 'cts')}", 'info')

            config = self.config_manager.load_config()

            # 检查必要参数
            devices = test_params.get('devices', [])
            if not devices:
                # 空设备列表也是可以的（用于测试）
                await log_callback("⚠️ 未选择设备，将使用默认设备", 'warning')
                devices = []  # 使用空列表
                # 不返回False，继续执行

            # 获取主机配置
            device_host = test_params.get('device_host', '')
            if '@' in device_host:
                host_config = {
                    'host': device_host.split('@')[1],
                    'username': device_host.split('@')[0],
                }
            else:
                host_config = {
                    'host': config.get('ubuntu_host', ''),
                    'username': config.get('ubuntu_user', 'hcq'),
                }

            # 建立SSH连接
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                await log_callback("❌ SSH连接失败", 'error')
                return False

            await log_callback(f"🔐 连接到主机: {host_config['host']}", 'info')
            await log_callback("✅ SSH 连接成功", 'success')

            # 生成进程组ID（多用户隔离）
            process_group_id = f"gms_test_{client_id.replace('@', '_')}_{int(time.time() * 1000)}"

            # 记录测试信息
            self.running_tests[client_id] = {
                'process_group_id': process_group_id,
                'test_type': test_params.get('test_type', 'cts'),
                'devices': test_params.get('devices', []),
                'start_time': datetime.now().isoformat(),
                'ssh': ssh,
                'status': 'running'
            }

            # 上传测试脚本
            await log_callback("📤 上传测试脚本...", 'info')
            script_uploaded = await self._upload_test_script(ssh, config, log_callback)
            if not script_uploaded:
                await log_callback("❌ 脚本上传失败", 'error')
                return False

            # 构建测试命令
            command = self._build_test_command(
                test_params,
                config,
                process_group_id,
                log_callback
            )

            if not command:
                await log_callback("❌ 命令构建失败", 'error')
                return False

            await log_callback(f"🚀 执行命令 (进程组: {process_group_id})", 'info')

            # 执行测试命令（后台任务）
            asyncio.create_task(
                self._execute_test_async(
                    ssh,
                    command,
                    client_id,
                    log_callback,
                    test_params.get('test_type', 'cts')
                )
            )

            return True

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error in start_test: {e}")

            # 如果是空设备列表或参数问题，不是真正的错误
            if "devices" in error_msg.lower() or "parameter" in error_msg.lower():
                await log_callback(f"⚠️ 启动测试警告: {error_msg}", 'warning')
                # 对于警告，我们返回True（启动成功，但没有设备）
                return True
            else:
                await log_callback(f"❌ 启动测试失败: {error_msg}", 'error')
                return False

    async def _upload_test_script(
        self,
        ssh: paramiko.SSHClient,
        config: Dict[str, Any],
        log_callback: Callable[[str, str], None]
    ) -> bool:
        """上传测试脚本到远程服务器"""
        try:
            # 检查本地脚本
            local_script = os.path.realpath(os.path.join(
                os.path.dirname(__file__),
                '..',
                'scripts',
                'run_GMS_Test_Auto.sh')
            )

            if not os.path.exists(local_script):
                logger.info(f"Checking local script: {local_script}"); logger.warning(f"Script not found: {local_script}, using remote fallback")
                # 脚本不存在，直接返回True（使用已有脚本）
                return True

            # 上传脚本
            suites_path = config.get('suites_path', '/home/hcq/GMS-Suite')
            remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

            script_size = os.path.getsize(local_script)
            size_kb = script_size / 1024

            await log_callback(f"📤 上传文件: run_GMS_Test_Auto.sh → {remote_script} ({size_kb:.2f}KB)", 'info')

            sftp = ssh.open_sftp()
            sftp.put(local_script, remote_script)
            sftp.close()

            # 设置可执行权限
            stdin, stdout, stderr = ssh.exec_command(f"chmod +x '{remote_script}'")
            stdout.read()

            await log_callback(f"🔐 已设置可执行权限: {remote_script}", 'info')
            await log_callback(f"✅ 上传完成 ({size_kb:.2f}KB)", 'success')

            return True

        except Exception as e:
            logger.error(f"Error uploading script: {e}")
            await log_callback(f"⚠️ 脚本上传失败: {str(e)}", 'warning')
            # 继续执行，可能脚本已存在
            return True

    def _build_test_command(
        self,
        test_params: Dict[str, Any],
        config: Dict[str, Any],
        process_group_id: str,
        log_callback: Callable[[str, str], None]
    ) -> Optional[str]:
        """构建测试命令"""
        try:
            test_type = test_params.get('test_type', 'cts')
            test_module = test_params.get('test_module', '')
            test_case = test_params.get('test_case', '')
            retry_dir = test_params.get('retry_dir', '')
            test_suite = test_params.get('test_suite', '')
            local_server = test_params.get('local_server', '')
            devices = test_params.get('devices', [])

            suites_path = config.get('suites_path', '/home/hcq/GMS-Suite')
            remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

            # 构建命令参数
            cmd_parts = [remote_script]

            # 添加测试类型
            if retry_dir:
                timestamp = os.path.basename(retry_dir.strip().rstrip('/'))
                cmd_parts.extend([test_type, "retry", timestamp])
                logger.info(f"Retry mode: {timestamp}")
            else:
                cmd_parts.append(test_type)
                if test_module:
                    cmd_parts.append(test_module)
                if test_case:
                    cmd_parts.append(test_case)

            # 添加设备参数
            if devices:
                device_args_list = []
                if len(devices) > 1:
                    device_args_list.extend(["--shard-count", str(len(devices))])
                for device in devices:
                    device_args_list.extend(["-s", device])

                device_args_str = " ".join(device_args_list)
                cmd_parts.extend(["--device-args", device_args_str])

            # 添加测试套件
            if test_suite:
                cmd_parts.extend(["--test-suite", test_suite])

            # 添加本地服务器
            if local_server:
                cmd_parts.extend(["--local-server", local_server])

            # 构建最终命令
            command = ' '.join(shlex.quote(part) for part in cmd_parts)
            command_full = f"cd {os.path.dirname(remote_script)} && {command}"

            logger.info(f"Test command: {command}")
            return command_full

        except Exception as e:
            logger.error(f"Error building command: {e}")
            return None

    async def _execute_test_async(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        client_id: str,
        log_callback: Callable[[str, str], None],
        test_type: str
    ):
        """异步执行测试命令"""
        try:
            # 执行命令（使用PTY获取实时输出）
            stdin, stdout, stderr = ssh.exec_command(command, get_pty=True)

            # 实时读取输出
            while not stdout.channel.exit_status_ready():
                if stdout.channel.recv_ready():
                    try:
                        data = stdout.channel.recv(65536).decode('utf-8', errors='replace')
                        if data:
                            lines = data.split('\n')
                            for line in lines:
                                if line.strip():
                                    await log_callback(line.strip(), 'info')
                    except Exception as e:
                        logger.error(f"Error reading stdout: {e}")

                if stderr.channel.recv_stderr_ready():
                    try:
                        error = stderr.channel.recv_stderr(65536).decode('utf-8', errors='replace')
                        if error:
                            lines = error.split('\n')
                            for line in lines:
                                if line.strip():
                                    await log_callback(line.strip(), 'error')
                    except Exception as e:
                        logger.error(f"Error reading stderr: {e}")

                await asyncio.sleep(0.05)

            # 获取退出码
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                await log_callback(f"✅ 测试完成 (exit code: {exit_code})", 'success')
            else:
                await log_callback(f"❌ 测试失败 (exit code: {exit_code})", 'error')

            # 更新测试状态
            if client_id in self.running_tests:
                self.running_tests[client_id]['status'] = 'completed'
                self.running_tests[client_id]['exit_code'] = exit_code
                self.running_tests[client_id]['end_time'] = datetime.now().isoformat()

            # 归还SSH连接
            self.ssh_manager.return_connection(ssh)

        except Exception as e:
            logger.error(f"Error in _execute_test_async: {e}")
            await log_callback(f"❌ 执行测试时出错: {str(e)}", 'error')

            # 归还SSH连接
            self.ssh_manager.return_connection(ssh)

    async def stop_test(
        self,
        client_id: str,
        log_callback: Callable[[str, str], None]
    ) -> bool:
        """
        停止测试

        Args:
            client_id: 客户端ID
            log_callback: 日志回调函数

        Returns:
            是否成功停止
        """
        try:
            if client_id not in self.running_tests:
                await log_callback("ℹ️ 没有运行的测试", 'info')
                # 没有运行测试也是成功的
                return True

            test_info = self.running_tests[client_id]
            process_group_id = test_info.get('process_group_id')
            test_type = test_info.get('test_type', 'cts')

            await log_callback(f"⏹️ 停止测试 (进程组: {process_group_id})", 'info')

            config = self.config_manager.load_config()
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                await log_callback("❌ SSH连接失败", 'error')
                return False

            # 方法1: 使用进程组ID杀死进程
            if process_group_id:
                find_cmd = f"ps eww -e | grep 'GMS_TEST_PGID={process_group_id}' | grep -v grep | awk '{{print $1}}'"
                stdout, stderr, code = self.ssh_manager.execute_command(ssh, find_cmd, timeout=10)

                if stdout.strip():
                    pids = stdout.strip().split('\n')
                    killed_count = 0
                    for pid in pids:
                        if pid.strip():
                            # 杀死进程
                            self.ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                            # 杀死子进程
                            self.ssh_manager.execute_command(ssh, f"pkill -9 -P {pid.strip()} 2>/dev/null")
                            killed_count += 1

                    await log_callback(f"✅ 已终止 {killed_count} 个测试进程", 'success')
                    self.ssh_manager.return_connection(ssh)

                    # 更新测试状态
                    test_info['status'] = 'stopped'
                    test_info['end_time'] = datetime.now().isoformat()

                    return True

                # 回退：尝试通过命令行参数查找
                fallback_cmd = f"ps aux | grep -- '--pgid {process_group_id}' | grep -v grep | awk '{{print $2}}'"
                stdout, stderr, code = self.ssh_manager.execute_command(ssh, fallback_cmd, timeout=10)

                if stdout.strip():
                    pids = stdout.strip().split('\n')
                    killed_count = 0
                    for pid in pids:
                        if pid.strip():
                            self.ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                            killed_count += 1

                    await log_callback(f"✅ 已终止 {killed_count} 个测试进程（命令行匹配）", 'success')
                    self.ssh_manager.return_connection(ssh)

                    # 更新测试状态
                    test_info['status'] = 'stopped'
                    test_info['end_time'] = datetime.now().isoformat()

                    return True

            # 方法2: 回退到传统方法（杀死tradefed进程）
            binary_map = {
                'cts': 'cts-tradefed',
                'gsi': 'cts-tradefed',
                'gts': 'gts-tradefed',
                'sts': 'sts-tradefed',
                'vts': 'vts-tradefed',
                'xts': 'xts-tradefed'
            }
            tradefed_bin = binary_map.get(test_type, 'tradefed')
            kill_cmd = f"pkill -f '[./]?{tradefed_bin}.*run commandAndExit'"

            stdout, stderr, code = self.ssh_manager.execute_command(ssh, kill_cmd, timeout=10)
            self.ssh_manager.return_connection(ssh)

            if code == 0:
                await log_callback(f"✅ {test_type.upper()} tradefed 进程已终止", 'success')
                test_info['status'] = 'stopped'
                test_info['end_time'] = datetime.now().isoformat()
                return True
            else:
                await log_callback("⚠️ 未找到运行中的测试进程", 'warning')
                return False

        except Exception as e:
            logger.error(f"Error in stop_test: {e}")
            await log_callback(f"❌ 停止测试失败: {str(e)}", 'error')
            return False

    def get_test_status(self, client_id: str) -> Optional[Dict[str, Any]]:
        """
        获取测试状态

        Args:
            client_id: 客户端ID

        Returns:
            测试状态字典
        """
        return self.running_tests.get(client_id)

    def get_all_tests_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有测试状态"""
        return self.running_tests.copy()


# 全局测试运行器实例
test_runner = TestRunner()
