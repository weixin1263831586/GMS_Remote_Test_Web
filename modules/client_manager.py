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
                ssh.connect(
                    client_ip,
                    username=username,
                    password=password,
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30
                )
                stdout = ssh.exec_command('whoami')[1]
                detected_username = stdout.read().decode().strip().split('\\')[-1]
                ssh.close()

                # 保存凭据
                self.client_hosts[client_ip] = detected_username

                if not any(c.get('username') == username for c in self.ssh_credentials):
                    self.ssh_credentials.insert(0, {'username': username, 'password': password})

                # 只保存客户端相关配置
                dynamic_config = {
                    'client_hosts': self.client_hosts,
                    'client_ssh_credentials': self.ssh_credentials
                }
                self.save_client_info(dynamic_config)

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
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    client_ip,
                    username=cred['username'],
                    password=cred['password'],
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30
                )
                stdout = ssh.exec_command('whoami')[1]
                detected_username = stdout.read().decode().strip().split('\\')[-1]
                ssh.close()

                self.client_hosts[client_ip] = detected_username

                # 只保存客户端相关配置
                dynamic_config = {
                    'client_hosts': self.client_hosts,
                    'client_ssh_credentials': self.ssh_credentials
                }
                self.save_client_info(dynamic_config)

                return True, detected_username, None
            except Exception:
                continue

        # 如果客户端 IP 与 local_server 中的 IP 匹配，通过 SSH 获取真实登录用户
        local_server = config.get('local_server', '')
        if '@' in local_server:
            local_ip = local_server.split('@')[1]
            if client_ip == local_ip:
                # 尝试用已保存的凭据连接并执行 whoami
                for cred in self.ssh_credentials:
                    try:
                        ssh = paramiko.SSHClient()
                        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                        ssh.connect(
                            client_ip,
                            username=cred['username'],
                            password=cred['password'],
                            timeout=30,
                            banner_timeout=30,
                            auth_timeout=30
                        )
                        stdout = ssh.exec_command('whoami')[1]
                        real_username = stdout.read().decode().strip()
                        ssh.close()
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
