"""
GMS测试报告分析器 - 统一的报告解析模块
简化重构版本，整合了XML解析、文件处理和分析功能
"""

import xml.etree.ElementTree as ET
import os
import zipfile
import tarfile
import logging
import re
import glob
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
        # 从Result属性获取（优先检查suite_name）
        for attr in ['suite_name', 'suite', 'test_type', 'testType', 'type', 'Type']:
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


class HostLogParser:
    """HostLog解析器 - 统一处理CTS/VTS/GTS等测试套件的host_log分析"""

    def __init__(self):
        pass

    def parse_log_dir(self, log_dir: str) -> Optional[TestReport]:
        """解析日志目录"""
        try:
            # 查找host_log文件
            host_log_path = self._find_host_log(log_dir)
            if not host_log_path:
                return None

            with open(host_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                log_content = f.read()

            return self._parse_log_content(log_content, log_dir)
        except Exception as e:
            logger.error(f"HostLog解析失败: {e}")
            return None

    def _find_host_log(self, log_dir: str) -> Optional[str]:
        """查找host_log文件"""
        for root, dirs, files in os.walk(log_dir):
            for file in files:
                if file.startswith('host_log_') and file.endswith('.txt'):
                    return os.path.join(root, file)
        return None

    def _parse_log_content(self, log_content: str, log_dir: str) -> Optional[TestReport]:
        """解析日志内容"""
        # 提取测试类型
        test_type = self._extract_test_type(log_content, log_dir)

        # 提取设备信息
        device = self._extract_device_info(log_content)

        # 提取Android版本
        android_version = self._extract_android_version(log_content)

        # 提取开始时间
        start_time = self._extract_start_time(log_content, log_dir)

        # 提取失败信息
        failures = self._extract_failures(log_content)

        # 生成统计信息（优先从 "completed in" 行获取）
        total, pass_count, fail_count = self._generate_summary(log_content)

        # 如果统计信息不准确，使用实际提取的失败数修正
        if len(failures) > fail_count:
            fail_count = len(failures)
            total = pass_count + fail_count

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

    def _extract_test_type(self, log_content: str, log_dir: str) -> str:
        """提取测试类型"""
        # 从目录名判断
        dir_name = os.path.basename(log_dir).lower()
        if 'cts' in dir_name:
            return 'CTS'
        elif 'vts' in dir_name:
            return 'VTS'
        elif 'gts' in dir_name:
            return 'GTS'

        # 从日志内容判断
        if 'VTS' in log_content or 'vts' in log_content:
            return 'VTS'
        elif 'CTS' in log_content or 'cts' in log_content:
            return 'CTS'
        elif 'GTS' in log_content or 'gts' in log_content:
            return 'GTS'

        return 'UNKNOWN'

    def _extract_device_info(self, log_content: str) -> str:
        """提取设备信息"""
        # 查找设备序列号
        match = re.search(r'Device\s+([A-Z0-9_]+)', log_content)
        if match:
            return match.group(1)

        # 查找设备名称
        match = re.search(r'on device\s+[\'"]?([A-Za-z0-9_]+)', log_content)
        if match:
            return match.group(1)

        return 'Unknown'

    def _extract_android_version(self, log_content: str) -> str:
        """提取测试套件版本（如 VTS 16_r3）"""
        # 优先从测试套件路径提取版本（如 android-vts-16_r3）
        match = re.search(r'android-(?:vts|cts|gts)-(\d+(?:_\d+)?)', log_content)
        if match:
            return match.group(1)

        # 其次从 ro.build.version.sdk 提取
        match = re.search(r'ro\.build\.version\.sdk[=:](\d+)', log_content)
        if match:
            return match.group(1)

        return 'Unknown'

    def _extract_start_time(self, log_content: str, log_dir: str) -> str:
        """提取开始时间"""
        # 从目录名提取
        dir_name = os.path.basename(log_dir)
        match = re.search(r'(\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2})', dir_name)
        if match:
            return match.group(1)

        # 从日志第一行提取
        lines = log_content.split('\n')
        for line in lines[:10]:
            match = re.search(r'(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', line)
            if match:
                return match.group(1)

        return 'Unknown'

    def _extract_failures(self, log_content: str) -> List[TestFailure]:
        """提取失败信息"""
        failures = []
        lines = log_content.split('\n')

        current_module = 'vts'  # 默认模块名
        detailed_module = None  # 详细模块名(如 VtsHalBluetoothTargetTest)

        i = 0

        while i < len(lines):
            line = lines[i]

            # 检测模块开始 - 提取详细模块名
            if 'TestInvocation: Starting invocation for' in line:
                match = re.search(r"Starting invocation for '(\w+)'", line)
                if match:
                    current_module = match.group(1)

            # 检测ModuleListener.testRunStarted行，获取详细模块名
            if 'ModuleListener.testRunStarted(' in line:
                match = re.search(r'ModuleListener\.testRunStarted\((\w+),', line)
                if match:
                    detailed_module = match.group(1)

            # 从FAILURE行提取模块名（备用方案）
            if '] PerInstance/' in line or '] ' in line:
                # 格式: [1/1 arm64-v8a VtsHalBluetoothTargetTest RK3572GMS4] TestName fail: ...
                module_match = re.search(r'\[\d+/\d+\s+\w+\s+(\w+(?:Target)?)\s+\w+\]', line)
                if module_match:
                    detailed_module = module_match.group(1)

            # 检测失败（FAILURE 或 ASSUMPTION_FAILURE）
            if 'FAILURE:' in line or 'ASSUMPTION_FAILURE:' in line:
                # 收集多行失败信息
                failure_lines = [line]
                j = i + 1

                # 收集后续的非空行，直到遇到下一个FAILURE或空行
                while j < len(lines):
                    next_line = lines[j].strip()
                    # 停止条件：遇到新的FAILURE、空行、时间戳行等
                    if ('FAILURE:' in next_line or
                        not next_line or
                        next_line.startswith('[') or
                        next_line.startswith('TestInvocation') or
                        next_line.startswith('---') or
                        'completed in' in next_line or
                        'TestInvocation: Starting invocation' in next_line):
                        break
                    failure_lines.append(lines[j])
                    j += 1

                # 组合多行失败信息
                full_failure_text = '\n'.join(failure_lines)

                # 使用详细模块名（如果可用）
                module_to_use = detailed_module if detailed_module else current_module

                if 'ASSUMPTION_FAILURE:' in line:
                    failure = self._parse_assumption_failure(full_failure_text, module_to_use)
                else:
                    failure = self._parse_failure_line(full_failure_text, module_to_use)

                if failure:
                    failures.append(failure)

                # 跳过已处理的行
                i = j - 1

            i += 1

        return failures

    def _parse_failure_line(self, line: str, module: str) -> TestFailure:
        """解析失败行（支持多行失败信息）"""
        test_name = 'Unknown'
        reason = ''
        stack_trace = ''

        # 分离reason和stack_trace
        lines = line.split('\n')

        # 提取测试信息 - 优先从FAILURE行提取
        # 尝试多种格式
        # 格式1: ClassName#MethodName (最常见)
        # 格式2: PerInstance/ClassName#MethodName/instance_id (VTS格式)
        # 格式3: module.ClassName#methodName

        # 先尝试从FAILURE行提取
        failure_line = lines[0]

        # 匹配完整的测试名称（包括PerInstance/前缀和instance后缀）
        # 例如: PerInstance/BluetoothAidlTest#Vsr_Bluetooth5Requirements/0_android_hardware_bluetooth_IBluetoothHci_default
        # 例如: Supplicant/SupplicantP2pIfaceAidlTest#RegisterCallback/0_android_hardware_wifi_supplicant_ISupplicant_default
        full_test_match = re.search(r'(\w+(?:/\w+)*?)/(\w+)#\w+[/\w]*', failure_line)
        if full_test_match:
            # 提取完整路径
            test_path = failure_line[failure_line.find(full_test_match.group(1)):]
            # 提取到fail:或FAILURE:之前的部分
            for marker in [' fail:', ' FAILURE:', '\n']:
                if marker in test_path:
                    test_path = test_path.split(marker)[0]
                    break
            test_name = test_path.strip()

            # 移除模块名前缀（如果有）
            # ModuleListener格式: "ModuleName Test/Class#Method" 需要移除 "ModuleName "
            # 检查是否以模块名开头
            if module and test_name.startswith(module + ' '):
                test_name = test_name[len(module + ' '):]
            # 也处理常见的Target后缀模块名
            elif module and test_name.startswith(module.replace('Target', '') + ' '):
                test_name = test_name[len(module.replace('Target', '') + ' '):]
            # 通用模式：移除开头的任何单词+空格（如果它看起来像模块名）
            else:
                # 检查是否以类似模块名的词开头后跟空格
                parts = test_name.split(None, 1)
                if len(parts) == 2 and '/' in parts[1]:
                    # 第二部分包含斜杠，很可能是真正的测试名称
                    test_name = parts[1]
        else:
            # 回退到简单格式: ClassName#methodName
            test_match = re.search(r'([\w.]+)#(\w+(?:\[.*?\])?)', failure_line)
            if test_match:
                test_name = f"{test_match.group(1)}#{test_match.group(2)}"
            elif '/' in failure_line:
                # 尝试其他格式
                parts = failure_line.split('/')
                if len(parts) >= 2:
                    test_name = f"{parts[-2].strip()}#{parts[-1].split()[0].strip()}"

        # 提取错误信息
        if 'FAILURE:' in lines[0]:
            parts = lines[0].split('FAILURE:', 1)
            if len(parts) > 1:
                reason = parts[1].strip()
        elif ' fail:' in lines[0]:
            parts = lines[0].split(' fail:', 1)
            if len(parts) > 1:
                reason = parts[1].strip()

        # 如果有多行信息，将第二行及以后的内容追加到reason中
        # 因为完整的失败信息可能跨多行（包括Value of, Actual, Expected等）
        if len(lines) > 1:
            additional_info = '\n'.join(lines[1:]).strip()
            if additional_info:
                reason = reason + '\n' + additional_info if reason else additional_info

        # stack_trace 保留完整的原始信息（用于深度分析）
        if len(lines) > 1:
            stack_trace = '\n'.join(lines[1:]).strip()
        else:
            # 单行情况，stack_trace也为空，因为所有信息都在reason中了
            stack_trace = ''

        return TestFailure(
            name=test_name,
            reason=reason,
            module=module,
            stack_trace=stack_trace
        )

    def _parse_assumption_failure(self, line: str, module: str) -> TestFailure:
        """解析假设失败（支持多行失败信息）"""
        test_name = 'Unknown'
        reason = ''
        stack_trace = ''

        # 提取测试信息
        test_match = re.search(r'([\w.]+)#(\w+(?:\[.*?\])?)', line)
        if test_match:
            test_name = f"{test_match.group(1)}#{test_match.group(2)}"

        # 分离reason和stack_trace
        lines = line.split('\n')
        if 'ASSUMPTION_FAILURE:' in lines[0]:
            parts = lines[0].split('ASSUMPTION_FAILURE:', 1)
            if len(parts) > 1:
                reason = parts[1].strip()

        # 如果有多行信息，将第二行及以后的内容追加到reason中
        if len(lines) > 1:
            additional_info = '\n'.join(lines[1:]).strip()
            if additional_info:
                reason = reason + '\n' + additional_info if reason else additional_info
            stack_trace = '\n'.join(lines[1:]).strip()
        else:
            stack_trace = ''

        return TestFailure(
            name=test_name,
            reason=reason,
            module=module,
            stack_trace=stack_trace
        )

    def _generate_summary(self, log_content: str) -> Tuple[int, int, int]:
        """生成测试摘要"""
        total = 0
        passed = 0
        failed = 0

        # 统计每个模块的结果
        matches = re.finditer(
            r'(\w+(?:\.\w+)*) completed in \d+ ms\. (\d+) passed, (\d+) failed, (\d+) not executed',
            log_content
        )

        for match in matches:
            module_passed = int(match.group(2))
            module_failed = int(match.group(3))
            passed += module_passed
            failed += module_failed
            total += module_passed + module_failed

        return total, passed, failed


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

    def find_host_log(self) -> Optional[str]:
        """查找host_log文件"""
        host_logs = glob.glob(os.path.join(self.temp_dir, '**/host_log_*.txt'), recursive=True)
        return host_logs[0] if host_logs else None


class ReportAnalyzer:
    """报告分析器主类 - 对外统一接口"""

    def __init__(self, temp_dir: str = '/tmp/gms_report'):
        self.temp_dir = temp_dir
        self.parser = XMLReportParser()
        self.host_log_parser = HostLogParser()
        self.file_handler = ReportFileHandler(temp_dir)

    def analyze_file(self, file_path: str) -> Optional[Dict]:
        """分析报告文件"""
        # 确保临时目录存在
        os.makedirs(self.temp_dir, exist_ok=True)

        # 如果是压缩包，先解压
        if file_path.endswith(('.zip', '.tar.gz', '.tgz')):
            if not self.file_handler.extract_archive(file_path):
                return None
            # 优先查找XML文件
            xml_path = self.file_handler.find_xml_file()
            if xml_path:
                report = self.parser.parse_file(xml_path)
                if report:
                    return self._report_to_dict(report)

            # 如果没有XML，尝试分析host_log
            host_log_path = self.file_handler.find_host_log()
            if host_log_path:
                report = self.host_log_parser.parse_log_dir(self.temp_dir)
                if report:
                    return self._report_to_dict(report)

            return None
        elif file_path.endswith('.xml'):
            report = self.parser.parse_file(file_path)
        else:
            logger.error(f"不支持的文件格式: {file_path}")
            return None

        if report:
            return self._report_to_dict(report)
        return None

    def analyze_log_dir(self, log_dir: str) -> Optional[Dict]:
        """分析日志目录（CTS/VTS/GTS日志）"""
        report = self.host_log_parser.parse_log_dir(log_dir)
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
                    'module': f.module,
                    'stack_trace': f.stack_trace
                }
                for f in report.failures
            ]
        }


# 全局实例
analyzer = ReportAnalyzer()
