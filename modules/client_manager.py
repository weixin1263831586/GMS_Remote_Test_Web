#!/usr/bin/env python3
"""
客户端管理模块
处理客户端IP检测、用户识别等功能
"""

import socket
import paramiko
from typing import Dict, Optional, Any
from core.config import config_manager


class ClientManager:
    """客户端管理器"""

    def __init__(self):
        self.client_hosts: Dict[str, str] = {}  # {client_ip: username}
        self.ssh_credentials: list = []  # 保存的SSH凭据

    def load_client_info(self) -> Dict[str, Any]:
        """加载客户端信息"""
        config = config_manager.load_config()
        self.client_hosts = config.get('client_hosts', {})
        self.ssh_credentials = config.get('client_ssh_credentials', [])
        return config

    def save_client_info(self, config: Dict[str, Any]) -> bool:
        """保存客户端信息"""
        return config_manager.save_dynamic_config(config)

    def get_client_ip(self, headers: Dict[str, str], remote_addr: str) -> str:
        """获取客户端IP地址"""
        client_ip = (
            headers.get('X-Forwarded-For', '').split(',')[0].strip() or
            headers.get('X-Real-IP') or
            remote_addr
        )
        return client_ip

    def detect_username(
        self,
        client_ip: str,
        username: Optional[str] = None,
        password: Optional[str] = None
    ) -> tuple[bool, str, Optional[str]]:
        """
        自动检测客户端用户名

        返回: (success, username, error_message)
        """
        config = self.load_client_info()

        # 手动SSH凭据
        if username and password:
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(client_ip, username=username, password=password, timeout=10)
                stdout = ssh.exec_command('whoami')[1]
                detected_username = stdout.read().decode().strip().split('\\')[-1]
                ssh.close()

                # 保存凭据
                self.client_hosts[client_ip] = detected_username
                config['client_hosts'] = self.client_hosts

                if not any(c.get('username') == username for c in self.ssh_credentials):
                    self.ssh_credentials.insert(0, {'username': username, 'password': password})
                config['client_ssh_credentials'] = self.ssh_credentials

                config['device_host'] = f'{detected_username}@{client_ip}'
                self.save_client_info(config)

                return True, detected_username, None
            except Exception as e:
                return False, '', str(e)

        # 检查已保存的映射
        if client_ip in self.client_hosts:
            detected_username = self.client_hosts[client_ip]
            config['device_host'] = f'{detected_username}@{client_ip}'
            self.save_client_info(config)
            return True, detected_username, None

        # 尝试已保存的SSH凭据
        for cred in self.ssh_credentials:
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    client_ip,
                    username=cred['username'],
                    password=cred['password'],
                    timeout=5
                )
                stdout = ssh.exec_command('whoami')[1]
                detected_username = stdout.read().decode().strip().split('\\')[-1]
                ssh.close()

                self.client_hosts[client_ip] = detected_username
                config['client_hosts'] = self.client_hosts
                config['device_host'] = f'{detected_username}@{client_ip}'
                self.save_client_info(config)

                return True, detected_username, None
            except Exception:
                continue

        return False, '', '请提供SSH凭据'

    def get_client_id(self, client_ip: str, username: str = 'unknown') -> str:
        """获取客户端ID"""
        return f"{username}@{client_ip}"


# 全局实例
client_manager = ClientManager()
