#!/usr/bin/env python3
"""
客户端管理模块
处理客户端IP检测、用户识别等功能
"""

from typing import Any

import paramiko

from foundation.common_utils import CommonUtils


class ClientManager:
    """客户端管理器"""

    # SSH connection timeout settings (reduced from 30s for faster failover)
    SSH_TIMEOUT = 3
    SSH_BANNER_TIMEOUT = 3
    SSH_AUTH_TIMEOUT = 3

    def __init__(self):
        self.config_manager = None
        self.client_hosts: dict[str, str] = {}  # {client_ip: username}
        self.ssh_credentials: list = []  # 保存的SSH凭据

    def load_client_info(self) -> dict[str, Any]:
        """加载客户端信息"""
        config = self.config_manager.load_config()
        self.client_hosts = config.get('client_hosts', {})
        self.ssh_credentials = config.get('client_ssh_credentials', [])
        return config

    def _save_client_runtime(self) -> bool:
        """保存客户端运行时配置"""
        runtime_config = self.config_manager.prepare_client_config({
            'client_hosts': self.client_hosts,
            'client_ssh_credentials': self.ssh_credentials,
        })
        return self.config_manager.save_runtime_config(runtime_config)

    def _ssh_whoami(self, client_ip: str, username: str, password: str) -> str:
        """Connect via SSH, run whoami, return username. Raises on failure."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                client_ip,
                username=username,
                password=password,
                timeout=self.SSH_TIMEOUT,
                banner_timeout=self.SSH_TIMEOUT,
                auth_timeout=self.SSH_AUTH_TIMEOUT
            )
            raw = ssh.exec_command('whoami')[1].read()
            return raw.decode("utf-8", errors="ignore").strip().split("\\")[-1]
        finally:
            ssh.close()

    def detect_username(
        self,
        client_ip: str,
        username: str | None = None,
        password: str | None = None
    ) -> tuple[bool, str, str | None]:
        """
        自动检测客户端用户名

        返回: (success, username, error_message)
        """
        config = self.load_client_info()

        # 手动SSH凭据
        if username and password:
            try:
                detected_username = self._ssh_whoami(client_ip, username, password)

                # 保存凭据
                self.client_hosts[client_ip] = detected_username

                if not any(c.get('username') == username for c in self.ssh_credentials):
                    self.ssh_credentials.insert(0, {'username': username, 'password': password})

                self._save_client_runtime()

                return True, detected_username, None
            except Exception as e:
                error_msg = str(e)
                # 提供更友好的错误提示
                if 'banner' in error_msg.lower() or 'timeout' in error_msg.lower():
                    return False, '', f'SSH 连接超时：请检查 {client_ip} 是否开启 SSH 服务，或网络是否通畅'
                elif 'authentication' in error_msg.lower() or 'password' in error_msg.lower():
                    return False, '', 'SSH 认证失败：请检查用户名和密码是否正确'
                elif 'connection refused' in error_msg.lower():
                    return False, '', f'SSH 连接被拒绝：{client_ip} 未开启 SSH 服务（端口 22）'
                return False, '', error_msg

        # 检查已保存的映射
        if client_ip in self.client_hosts:
            detected_username = self.client_hosts[client_ip]
            return True, detected_username, None

        # 尝试已保存的SSH凭据
        for cred in self.ssh_credentials:
            try:
                detected_username = self._ssh_whoami(client_ip, cred['username'], cred['password'])

                self.client_hosts[client_ip] = detected_username

                self._save_client_runtime()

                return True, detected_username, None
            except Exception:
                continue

        # 如果客户端 IP 与 local_server 中的 IP 匹配，通过 SSH 获取真实登录用户
        local_server = config.get('local_server', '')
        if '@' in local_server:
            local_ip = CommonUtils.extract_ip_from_host(local_server)
            if client_ip == local_ip:
                # 尝试用已保存的凭据连接并执行 whoami
                for cred in self.ssh_credentials:
                    try:
                        real_username = self._ssh_whoami(client_ip, cred['username'], cred['password'])
                        return True, real_username, None
                    except Exception:
                        continue

        # 注意：不要使用 ubuntu_user 作为客户端用户名的默认值
        # ubuntu_user 只用于服务器端操作，不应该用于客户端身份识别
        return False, '', '无法自动检测用户名'

    def get_client_id(self, client_ip: str, username: str = 'unknown') -> str:
        """获取客户端ID"""
        return f"{username}@{client_ip}"


# 全局实例
client_manager = ClientManager()
