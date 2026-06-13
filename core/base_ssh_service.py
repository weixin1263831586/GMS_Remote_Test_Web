"""SSH基础服务类 - 为各管理器类提供统一的SSH连接管理和命令执行接口。"""
import logging
from typing import Optional, Dict, Any, Tuple
from paramiko import SSHClient

from .ssh import ssh_manager
from .config import config_manager

logger = logging.getLogger(__name__)


class BaseSSHService:
    """SSH基础服务类 - 提供统一的SSH连接管理、命令执行和错误处理。"""

    def __init__(self):
        """初始化基础服务"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager

    def get_ssh_connection(self, config: Optional[dict] = None) -> Optional[SSHClient]:
        """获取SSH连接。config 为 None 时使用默认配置。"""
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
        """执行SSH命令，返回 (stdout, stderr, exit_code)。"""
        return self.ssh_manager.execute_command(ssh, command, timeout, get_pty)

    def return_ssh_connection(self, ssh: Optional[SSHClient]):
        """归还SSH连接到连接池。"""
        if ssh:
            self.ssh_manager.return_connection(ssh)

    def execute_with_connection(
        self,
        command: str,
        config: Optional[dict] = None,
        timeout: int = 30,
        get_pty: bool = False
    ) -> Tuple[str, str, int]:
        """获取连接、执行命令、归还连接。返回 (stdout, stderr, exit_code)。"""
        ssh = None
        try:
            ssh = self.get_ssh_connection(config)
            if not ssh:
                return '', 'SSH连接失败', -1
            return self.execute_ssh_command(ssh, command, timeout, get_pty)
        finally:
            self.return_ssh_connection(ssh)

    def create_success_result(self, message: str = '', data: Any = None) -> Dict[str, Any]:
        """创建标准成功结果字典。"""
        result = {'success': True, 'message': message}
        if data is not None:
            result['data'] = data
        return result

    def create_error_result(self, error: str, data: Any = None) -> Dict[str, Any]:
        """创建标准错误结果字典。"""
        result = {'success': False, 'error': error}
        if data is not None:
            result['data'] = data
        return result
