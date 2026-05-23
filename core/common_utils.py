"""
通用工具函数 - 提供项目范围内的通用工具方法

整合重复的主机地址解析、本地检查等逻辑
"""
import socket
import logging
import re
from typing import Tuple, Optional, Dict, Any
from urllib.parse import urlparse

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
    def sanitize_url(cls, url: str) -> str:
        """
        清理和标准化URL

        Args:
            url: 原始URL

        Returns:
            清理后的URL

        Examples:
            >>> CommonUtils.sanitize_url("view-source:https://example.com")
            'https://example.com'
            >>> CommonUtils.sanitize_url("https://example.com/")
            'https://example.com/'
        """
        if not url:
            return url

        # 移除常见的浏览器前缀
        prefixes_to_remove = ['view-source:', 'view-source://', 'about:', 'about://']
        for prefix in prefixes_to_remove:
            if url.startswith(prefix):
                url = url[len(prefix):]
                break

        # 验证URL格式
        try:
            parsed = urlparse(url)
            if not parsed.scheme:
                # 如果没有协议，尝试添加https
                url = f"https://{url}"
            elif parsed.scheme not in ['http', 'https']:
                logger.warning(f"Unexpected URL scheme: {parsed.scheme}")
        except Exception as e:
            logger.warning(f"Invalid URL format: {url}, error: {e}")

        return url

    @classmethod
    def validate_url(cls, url: str) -> bool:
        """
        验证URL格式是否有效

        Args:
            url: 待验证的URL

        Returns:
            URL是否有效
        """
        if not url:
            return False

        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc]) and result.scheme in ['http', 'https']
        except Exception:
            return False

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


class StackTraceUtils:
    """堆栈跟踪解析工具类"""

    # 排除的工具类（这些类通常不是真正的失败位置）
    EXCLUDED_CLASSES = {
        'Assert', 'TestRunner', 'TestCase', 'TestUtil', 'CtsTestUtil',
        'Mock', 'FrameworkMethod', 'Failures'
    }

    # 预编译正则表达式（性能优化）
    FAILURE_LOCATION_PATTERNS = [
        # 优先匹配测试类（com.android.xxx.TestClass.method(TestFile.java:line)）
        re.compile(r'at\s+([a-z][a-z0-9.]*)\.([A-Z][\w]*)\.(\w+)\(([\w.$]+)\.(kt|java):(\d+)\)'),
        # 备用：直接匹配文件名
        re.compile(r'\(([\w.$]+)\.(kt|java):(\d+)\)'),
    ]

    @classmethod
    def extract_failure_location(cls, stack_trace: str) -> Optional[Dict[str, str]]:
        """
        从堆栈跟踪中提取失败位置信息（优先提取测试类，排除工具类）

        Args:
            stack_trace: 堆栈跟踪字符串

        Returns:
            dict with keys: file_name, file_type, line_number
            或 None（如果无法提取）

        Examples:
            >>> extract_failure_location("at com.android.gpu.vts.OpenGlEsTest.checkOpenGlEsDeqpLevelIsHighEnough(OpenGlEsTest.java:77)")
            {'file_name': 'OpenGlEsTest', 'file_type': 'java', 'line_number': '77'}
        """
        if not stack_trace:
            return None

        # 收集所有匹配项
        all_matches = []
        for pattern in cls.FAILURE_LOCATION_PATTERNS:
            for match in pattern.finditer(stack_trace):
                all_matches.append(match)

        # 优先返回测试类的位置（排除工具类）
        for match in all_matches:
            groups = match.groups()
            # 根据不同的模式提取文件名
            if len(groups) >= 5:  # 完整模式：package, class, method, file, ext, line
                file_name = groups[3]
                file_type = groups[4]
                line_number = groups[5]
                class_name = groups[1]

                # 跳过工具类
                if class_name in cls.EXCLUDED_CLASSES or file_name in cls.EXCLUDED_CLASSES:
                    continue

                return {
                    'file_name': file_name,
                    'file_type': file_type,
                    'line_number': line_number
                }
            elif len(groups) >= 3:  # 简单模式：(file.ext:line)
                file_name = groups[0]
                file_type = groups[1]
                line_number = groups[2]

                # 跳过工具类
                if file_name in cls.EXCLUDED_CLASSES:
                    continue

                return {
                    'file_name': file_name,
                    'file_type': file_type,
                    'line_number': line_number
                }

        # 如果没有找到测试类，返回第一个非工具类的位置
        for match in all_matches:
            groups = match.groups()
            if len(groups) >= 3:
                file_name = groups[0] if len(groups) < 5 else groups[3]
                file_type = groups[1] if len(groups) < 5 else groups[4]
                line_number = groups[2] if len(groups) < 5 else groups[5]

                if file_name not in cls.EXCLUDED_CLASSES:
                    return {
                        'file_name': file_name,
                        'file_type': file_type,
                        'line_number': line_number
                    }

        return None
