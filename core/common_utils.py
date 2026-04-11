"""
通用工具函数 - 提供项目范围内的通用工具方法

整合重复的主机地址解析、本地检查等逻辑
"""
import socket
import logging
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)


class CommonUtils:
    """通用工具类"""

    # 本地主机标识列表
    LOCAL_HOSTS = ['localhost', '127.0.0.1', '::1']

    @classmethod
    def get_local_ip(cls) -> Optional[str]:
        """
        获取本机IP地址

        Returns:
            本机IP地址，失败返回None
        """
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception as e:
            logger.warning(f"Failed to get local IP: {e}")
            return None

    @classmethod
    def is_local_host(cls, host: str) -> bool:
        """
        检查是否为本地主机

        Args:
            host: 主机地址

        Returns:
            是否为本地主机
        """
        local_hosts = cls.LOCAL_HOSTS.copy()
        local_ip = cls.get_local_ip()
        if local_ip:
            local_hosts.append(local_ip)

        return host in local_hosts

    @classmethod
    def parse_host_address(cls, host: str) -> Tuple[Optional[str], str]:
        """
        解析主机地址

        Args:
            host: 主机地址，可能包含 username@ 前缀

        Returns:
            (username, host_ip) 元组
            如果没有 username，返回 (None, host)

        Examples:
            >>> parse_host_address('user@192.168.1.1')
            ('user', '192.168.1.1')
            >>> parse_host_address('192.168.1.1')
            (None, '192.168.1.1')
        """
        if '@' in host:
            username, host_ip = host.split('@', 1)
            return username, host_ip
        return None, host

    @classmethod
    def create_result_dict(
        cls,
        success: bool = True,
        message: str = '',
        error: str = '',
        data: Any = None
    ) -> Dict[str, Any]:
        """
        创建标准结果字典

        Args:
            success: 是否成功
            message: 消息
            error: 错误信息
            data: 附加数据

        Returns:
            标准格式的结果字典
        """
        result = {'success': success}

        if message:
            result['message'] = message

        if not success and error:
            result['error'] = error
        elif not success and message:
            result['error'] = message

        if data is not None:
            result['data'] = data

        return result

    @classmethod
    def create_success_result(
        cls,
        message: str = '',
        data: Any = None
    ) -> Dict[str, Any]:
        """
        创建成功结果字典

        Args:
            message: 成功消息
            data: 附加数据

        Returns:
            标准成功结果字典
        """
        return cls.create_result_dict(True, message, '', data)

    @classmethod
    def create_error_result(
        cls,
        error: str,
        data: Any = None
    ) -> Dict[str, Any]:
        """
        创建错误结果字典

        Args:
            error: 错误信息
            data: 附加数据

        Returns:
            标准错误结果字典
        """
        return cls.create_result_dict(False, '', error, data)

    @staticmethod
    def sanitize_path(path: str, default_user: str = 'hcq') -> str:
        """
        清理路径中的占位符

        Args:
            path: 包含占位符的路径（如 ${ubuntu_user}）
            default_user: 默认用户名

        Returns:
            替换后的路径
        """
        if '${ubuntu_user}' in path:
            return path.replace('${ubuntu_user}', default_user)
        return path

    @staticmethod
    def format_command_output(
        stdout: str,
        stderr: str = '',
        exit_code: int = 0
    ) -> str:
        """
        格式化命令输出

        Args:
            stdout: 标准输出
            stderr: 标准错误
            exit_code: 退出码

        Returns:
            格式化后的输出字符串
        """
        output = []
        if stdout:
            output.append(f"STDOUT:\n{stdout}")
        if stderr:
            output.append(f"STDERR:\n{stderr}")
        if exit_code != 0:
            output.append(f"Exit Code: {exit_code}")
        return '\n'.join(output) if output else '(no output)'

    @staticmethod
    def extract_ip_from_host(host: str) -> str:
        """
        从主机地址中提取 IP 部分

        Args:
            host: 主机地址，可能包含 username@ 前缀

        Returns:
            IP 地址部分

        Examples:
            >>> extract_ip_from_host('user@192.168.1.1')
            '192.168.1.1'
            >>> extract_ip_from_host('192.168.1.1')
            '192.168.1.1'
        """
        if '@' in host:
            return host.split('@', 1)[1]
        return host
