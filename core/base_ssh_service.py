"""
SSH基础服务类 - 提供统一的SSH操作接口

为各管理器类提供统一的SSH连接管理和命令执行接口
"""
import logging
from typing import Optional, Dict, Any, Tuple
from paramiko import SSHClient

from .ssh import ssh_manager
from .config import config_manager

logger = logging.getLogger(__name__)


class BaseSSHService:
    """
    SSH基础服务类

    提供统一的SSH连接管理、命令执行和错误处理
    各管理器类可以继承此类以获得SSH操作能力
    """

    def __init__(self):
        """初始化基础服务"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager

    def get_ssh_connection(self, config: Optional[dict] = None) -> Optional[SSHClient]:
        """
        获取SSH连接

        Args:
            config: 配置字典，如果为None则使用默认配置

        Returns:
            SSHClient 对象，失败返回 None
        """
        if config is None:
            config = self.config_manager.load_config()
        return self.ssh_manager.get_connection(config)

    def execute_ssh_command(
        self,
        ssh: SSHClient,
        command: str,
        timeout: int = 30,
        get_pty: bool = False
    ) -> Tuple[str, str, int]:
        """
        执行SSH命令

        Args:
            ssh: SSHClient 对象
            command: 要执行的命令
            timeout: 超时时间（秒）
            get_pty: 是否获取伪终端

        Returns:
            (stdout, stderr, exit_code)
        """
        return self.ssh_manager.execute_command(ssh, command, timeout, get_pty)

    def return_ssh_connection(self, ssh: Optional[SSHClient]):
        """
        归还SSH连接

        Args:
            ssh: SSHClient 对象，可以为None
        """
        if ssh:
            self.ssh_manager.return_connection(ssh)

    def execute_with_connection(
        self,
        command: str,
        config: Optional[dict] = None,
        timeout: int = 30,
        get_pty: bool = False
    ) -> Tuple[str, str, int]:
        """
        获取连接、执行命令、归还连接（一站式服务）

        Args:
            command: 要执行的命令
            config: 配置字典
            timeout: 超时时间
            get_pty: 是否获取伪终端

        Returns:
            (stdout, stderr, exit_code)
        """
        ssh = None
        try:
            ssh = self.get_ssh_connection(config)
            if not ssh:
                return '', 'SSH连接失败', -1

            return self.execute_ssh_command(ssh, command, timeout, get_pty)
        finally:
            self.return_ssh_connection(ssh)

    def create_success_result(
        self,
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
        result = {'success': True, 'message': message}
        if data is not None:
            result['data'] = data
        return result

    def create_error_result(
        self,
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
        result = {'success': False, 'error': error}
        if data is not None:
            result['data'] = data
        return result

    def safe_execute(
        self,
        func,
        *args,
        error_message: str = '操作失败',
        **kwargs
    ) -> Dict[str, Any]:
        """
        安全执行函数，自动捕获异常

        Args:
            func: 要执行的函数
            *args: 函数参数
            error_message: 错误消息前缀
            **kwargs: 函数关键字参数

        Returns:
            结果字典
        """
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"{error_message}: {e}")
            return self.create_error_result(f"{error_message}: {str(e)}")
