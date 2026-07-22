"""项目通用工具函数。"""
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ANSI 转义序列匹配（CSI、OSC、VT100 码）
_ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\x1b\[.*?[a-zA-Z]')


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape sequences from text (CSI, OSC, and other VT100 codes)."""
    return _ANSI_ESCAPE_PATTERN.sub('', text)


class CommonUtils:
    """通用工具类"""

    @staticmethod
    def decode_ssh_output(data: bytes) -> str:
        """Decode SSH output, trying UTF-8 first then GBK for Windows hosts."""
        for encoding in ('utf-8', 'gbk', 'latin-1'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode('utf-8', errors='replace')

    @classmethod
    def get_local_ip(cls) -> str | None:
        """获取本机 IP，失败时返回 None。"""
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception as e:
            logger.warning(f"Failed to get local IP: {e}")
            return None

    @classmethod
    def sanitize_url(cls, url: str) -> str:
        """清理浏览器前缀并补全 URL 协议。"""
        if not url:
            return url

        # 移除浏览器前缀。
        prefixes_to_remove = ['view-source:', 'view-source://', 'about:', 'about://']
        for prefix in prefixes_to_remove:
            if url.startswith(prefix):
                url = url[len(prefix):]
                break

        try:
            parsed = urlparse(url)
            if not parsed.scheme:
                url = f"https://{url}"
            elif parsed.scheme not in ['http', 'https']:
                logger.warning(f"Unexpected URL scheme: {parsed.scheme}")
        except Exception as e:
            logger.warning(f"Invalid URL format: {url}, error: {e}")

        return url

    @classmethod
    def parse_host_address(cls, host: str) -> tuple[str | None, str]:
        """将 ``username@host`` 解析为用户名和主机。"""
        from foundation.networking import parse_host_address
        return parse_host_address(host)

    @classmethod
    def create_result_dict(
        cls,
        success: bool = True,
        message: str = '',
        error: str = '',
        data: Any = None
    ) -> dict[str, Any]:
        """创建标准结果字典。"""
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
    ) -> dict[str, Any]:
        """创建成功结果字典。"""
        return cls.create_result_dict(True, message, '', data)

    @classmethod
    def create_error_result(
        cls,
        error: str,
        data: Any = None
    ) -> dict[str, Any]:
        """创建错误结果字典。"""
        return cls.create_result_dict(False, '', error, data)

    @staticmethod
    def extract_ip_from_host(host: str) -> str:
        """从 ``username@host`` 中提取主机部分。"""
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
    def extract_failure_location(cls, stack_trace: str) -> dict[str, str] | None:
        """提取首个非工具类失败位置。"""
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
