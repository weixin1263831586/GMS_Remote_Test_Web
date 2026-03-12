"""
GMS测试报告分析器 - 统一的报告解析模块
简化重构版本，整合了XML解析、文件处理和分析功能
"""

import xml.etree.ElementTree as ET
import os
import zipfile
import tarfile
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class TestFailure:
    """测试失败信息数据类"""
    name: str
    reason: str
    module: str = '未知模块'
    stack_trace: str = ''


@dataclass
class TestReport:
    """测试报告数据类"""
    test_type: str
    device: str
    android_version: str
    start_time: str
    total: int
    pass_count: int
    fail_count: int
    pass_rate: str
    failures: List[TestFailure]


class XMLReportParser:
    """XML报告解析器 - 统一处理test_result.xml解析"""

    def __init__(self):
        self.parent_map = None

    def parse_file(self, xml_path: str) -> Optional[TestReport]:
        """解析XML文件"""
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML解析失败: {e}")
            return None

    def parse_content(self, xml_content: str) -> Optional[TestReport]:
        """解析XML内容字符串"""
        try:
            root = ET.fromstring(xml_content)
            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML内容解析失败: {e}")
            return None

    def _parse_root(self, root: ET.Element) -> Optional[TestReport]:
        """解析XML根节点"""
        # 构建父节点映射
        self.parent_map = {c: p for p in root.iter() for c in p}

        # 提取基本信息
        test_type = self._get_test_type(root)
        device = self._get_device_info(root)
        android_version = self._get_android_version(root)
        start_time = self._get_start_time(root)

        # 提取统计信息
        total, pass_count, fail_count = self._get_summary(root)

        # 解析失败的测试用例
        failures = self._parse_failures(root)

        # 计算通过率
        pass_rate = f"{(pass_count / total * 100):.2f}%" if total > 0 else "0%"

        return TestReport(
            test_type=test_type,
            device=device,
            android_version=android_version,
            start_time=start_time,
            total=total,
            pass_count=pass_count,
            fail_count=fail_count,
            pass_rate=pass_rate,
            failures=failures
        )

    def _get_test_type(self, root: ET.Element) -> str:
        """获取测试类型"""
        # 从Result属性获取
        for attr in ['test_type', 'testType', 'type', 'Type']:
            if root.get(attr):
                return root.get(attr)

        # 从Build节点获取
        build = root.find('.//Build')
        if build is not None:
            return build.get('test_type', build.get('testType', 'GTS'))

        return 'GTS'

    def _get_device_info(self, root: ET.Element) -> str:
        """获取设备信息"""
        # 优先从Result节点获取
        device = root.get('devices', '')
        if device:
            return device

        # 从Build节点获取
        build = root.find('.//Build')
        if build is not None:
            device = build.get('device_serial', build.get('serial', ''))
            if device:
                return device

        return '未知设备'

    def _get_android_version(self, root: ET.Element) -> str:
        """获取测试套件版本（suite_version）"""
        # 优先从Result根节点获取suite_version
        for attr in ['suite_version', 'android_version', 'AndroidVersion']:
            if root.get(attr):
                return root.get(attr)

        # 从Build节点获取
        build = root.find('.//Build')
        if build is not None:
            return build.get('version', build.get('sdk', '15'))
        return '15'

    def _get_start_time(self, root: ET.Element) -> str:
        """获取开始时间"""
        for attr in ['start_display', 'end_display', 'start_time', 'StartTime']:
            if root.get(attr):
                return root.get(attr)
        return '未知时间'

    def _get_summary(self, root: ET.Element) -> Tuple[int, int, int]:
        """获取摘要统计信息"""
        summary = root.find('.//Summary')
        if summary is not None:
            # Summary节点有 pass 和 failed 属性，total需要计算
            passed = int(summary.get('pass', summary.get('Passed', 0)))
            failed = int(summary.get('failed', summary.get('Failed', 0)))
            total = passed + failed
            return total, passed, failed

        # 如果没有Summary，手动统计
        return self._count_tests(root)

    def _count_tests(self, root: ET.Element) -> Tuple[int, int, int]:
        """手动统计测试用例"""
        test_cases = root.findall('.//Test')
        passed = sum(1 for tc in test_cases if tc.get('result', 'pass').lower() == 'pass')
        failed = sum(1 for tc in test_cases if tc.get('result', 'pass').lower() == 'fail')
        return len(test_cases), passed, failed

    def _parse_failures(self, root: ET.Element) -> List[TestFailure]:
        """解析失败的测试用例"""
        failures = []
        test_cases = root.findall('.//Test')

        for test_case in test_cases:
            result_attr = test_case.get('result', test_case.get('Result', 'pass'))
            outcome = test_case.get('outcome', test_case.get('Outcome', ''))

            if result_attr.lower() == 'fail' or outcome.lower() == 'fail':
                # 获取模块名
                module_name = self._get_module_name(test_case)

                # 获取测试名称
                test_name = self._get_test_name(test_case)

                # 获取失败原因和堆栈
                reason, stack_trace = self._get_failure_info(test_case)

                # 组合失败信息（去重）
                full_reason = self._combine_reason_stack(reason, stack_trace)

                failures.append(TestFailure(
                    name=test_name,
                    reason=full_reason,
                    module=module_name,
                    stack_trace=stack_trace
                ))

        return failures

    def _get_module_name(self, test_case: ET.Element) -> str:
        """获取测试所属模块"""
        current = test_case
        while current is not None:
            if current in self.parent_map:
                current = self.parent_map[current]
                if current is not None and current.tag == 'Module':
                    return current.get('name', '未知模块')
            else:
                break
        return '未知模块'

    def _get_test_name(self, test_case: ET.Element) -> str:
        """获取测试用例完整名称"""
        test_name = test_case.get('name', '未知用例')

        # 如果是Test节点，尝试组合完整名称
        if test_case.tag == 'Test' and test_case in self.parent_map:
            parent = self.parent_map[test_case]
            if parent is not None and parent.tag == 'TestCase':
                class_name = parent.get('name', '')
                if class_name and test_name:
                    return f"{class_name}#{test_name}"

        return test_name

    def _get_failure_info(self, test_case: ET.Element) -> Tuple[str, str]:
        """获取失败信息"""
        reason = ''
        stack_trace = ''

        # 从Failure节点获取
        failure = test_case.find('Failure')
        if failure is not None:
            reason = failure.get('message', '')
            if failure.text:
                stack_trace = failure.text.strip()

        # 从Error节点获取
        if not reason:
            error = test_case.find('Error')
            if error is not None:
                reason = error.get('message', '')
                if error.text:
                    stack_trace = error.text.strip()

        # 从StackTrace子节点获取
        if not stack_trace:
            stack_elem = test_case.find('.//StackTrace')
            if stack_elem is not None and stack_elem.text:
                stack_trace = stack_elem.text.strip()

        return reason or '无失败原因', stack_trace

    def _combine_reason_stack(self, reason: str, stack_trace: str) -> str:
        """组合失败原因和堆栈，避免重复"""
        if not stack_trace:
            return reason

        # 检查堆栈第一行是否就是reason
        stack_lines = stack_trace.strip().split('\n')
        if stack_lines and stack_lines[0].strip() == reason.strip():
            return stack_trace

        # 检查reason是否在堆栈中
        if reason in stack_trace:
            return stack_trace

        # 组合显示
        return f"{reason}\n\n{stack_trace}"


class ReportFileHandler:
    """报告文件处理器 - 统一处理文件解压和查找"""

    def __init__(self, temp_dir: str):
        self.temp_dir = temp_dir

    def extract_archive(self, archive_path: str) -> bool:
        """解压压缩包"""
        try:
            if archive_path.endswith('.zip'):
                self._extract_zip(archive_path)
            elif archive_path.endswith(('.tar.gz', '.tgz')):
                self._extract_tar(archive_path)
            else:
                logger.warning(f"不支持的压缩格式: {archive_path}")
                return False
            return True
        except Exception as e:
            logger.error(f"解压失败: {e}")
            return False

    def _extract_zip(self, zip_path: str):
        """解压ZIP文件"""
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(self.temp_dir)

    def _extract_tar(self, tar_path: str):
        """解压TAR文件"""
        with tarfile.open(tar_path, 'r:gz') as tf:
            tf.extractall(self.temp_dir)

    def find_xml_file(self) -> Optional[str]:
        """查找test_result.xml文件"""
        for root, dirs, files in os.walk(self.temp_dir):
            for file in files:
                if file == 'test_result.xml':
                    return os.path.join(root, file)
        return None


class ReportAnalyzer:
    """报告分析器主类 - 对外统一接口"""

    def __init__(self, temp_dir: str = '/tmp/gms_report'):
        self.temp_dir = temp_dir
        self.parser = XMLReportParser()
        self.file_handler = ReportFileHandler(temp_dir)

    def analyze_file(self, file_path: str) -> Optional[Dict]:
        """分析报告文件"""
        # 确保临时目录存在
        os.makedirs(self.temp_dir, exist_ok=True)

        # 如果是压缩包，先解压
        if file_path.endswith(('.zip', '.tar.gz', '.tgz')):
            if not self.file_handler.extract_archive(file_path):
                return None
            # 查找XML文件
            xml_path = self.file_handler.find_xml_file()
            if not xml_path:
                return None
            report = self.parser.parse_file(xml_path)
        elif file_path.endswith('.xml'):
            report = self.parser.parse_file(file_path)
        else:
            logger.error(f"不支持的文件格式: {file_path}")
            return None

        if report:
            return self._report_to_dict(report)
        return None

    def analyze_content(self, xml_content: str) -> Optional[Dict]:
        """分析XML内容"""
        report = self.parser.parse_content(xml_content)
        if report:
            return self._report_to_dict(report)
        return None

    def _report_to_dict(self, report: TestReport) -> Dict:
        """将报告对象转换为字典（兼容旧格式）"""
        return {
            'summary': {
                'total': report.total,
                'pass': report.pass_count,
                'fail': report.fail_count,
                'pass_rate': report.pass_rate
            },
            'details': {
                'test_type': report.test_type,
                'device': report.device,
                'android_version': report.android_version,
                'start_time': report.start_time
            },
            'failures': [
                {
                    'name': f.name,
                    'reason': f.reason,
                    'module': f.module
                }
                for f in report.failures
            ]
        }


# 全局实例
analyzer = ReportAnalyzer()
