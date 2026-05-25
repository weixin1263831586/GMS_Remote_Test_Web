"""
GMS测试报告分析器 - 统一的报告解析模块
整合了XML解析、文件处理和分析功能
"""

import os
import zipfile
import tarfile
import logging
import re
import glob
import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

# 优先使用lxml,如果不可用则回退到ElementTree
try:
    from lxml import etree
    USE_LXML = True
    logger = logging.getLogger(__name__)
    logger.info("使用lxml进行XML解析(高性能模式)")
except ImportError:
    import xml.etree.ElementTree as ET
    USE_LXML = False
    logger = logging.getLogger(__name__)
    logger.warning("lxml不可用,使用ElementTree(标准库模式)")

# 配置日志
logging.basicConfig(level=logging.INFO)


def get_opengrok_project_for_android_version(android_version: str, opengrok_config: dict) -> str:
    """
    根据Android版本获取对应的OpenGrok项目

    Args:
        android_version: Android版本字符串 (如 "16", "16.1", "16_r3")
        opengrok_config: OpenGrok配置字典

    Returns:
        对应的OpenGrok项目名，如果无法匹配则返回default_project
    """
    if not android_version or not opengrok_config:
        return opengrok_config.get('default_project', 'Android16')

    # 提取Android版本的主版本号
    import re
    match = re.match(r'(\d+)', android_version)
    if match:
        major_version = match.group(1)
        project_mapping = opengrok_config.get('project_mapping', {})

        # 根据主版本号查找对应项目
        if major_version in project_mapping:
            return project_mapping[major_version]

    # 如果无法匹配，返回默认项目
    return opengrok_config.get('default_project', 'Android16')


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
    suite_version: str      # 测试套件版本（如 16.1_r2）
    android_version: str    # Android版本（从 build_version_release 获取）
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
            if USE_LXML:
                # 使用lxml解析,支持更大的文件和更快的速度
                tree = etree.parse(xml_path, etree.XMLParser(remove_blank_text=True, huge_tree=True))
                root = tree.getroot()
            else:
                # 回退到ElementTree
                tree = ET.parse(xml_path)
                root = tree.getroot()

            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML解析失败: {e}")
            return None

    def parse_stream(self, xml_stream) -> Optional[TestReport]:
        """从文件流解析XML，避免为压缩包先解压落盘。"""
        try:
            if USE_LXML:
                tree = etree.parse(xml_stream, etree.XMLParser(remove_blank_text=True, huge_tree=True))
                root = tree.getroot()
            else:
                tree = ET.parse(xml_stream)
                root = tree.getroot()

            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML流解析失败: {e}")
            return None

    def parse_content(self, xml_content: str) -> Optional[TestReport]:
        """解析XML内容字符串"""
        try:
            if USE_LXML:
                root = etree.fromstring(xml_content.encode('utf-8'), etree.XMLParser(remove_blank_text=True, huge_tree=True))
            else:
                root = ET.fromstring(xml_content)

            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML内容解析失败: {e}")
            return None

    def _parse_root(self, root) -> Optional[TestReport]:
        """解析XML根节点"""
        # lxml元素自带getparent()，不需要为大报告构建全量父节点映射。
        if USE_LXML:
            self.parent_map = None
        else:
            self.parent_map = {c: p for p in root.iter() for c in p}

        # 提取基本信息
        test_type = self._get_test_type(root)
        device = self._get_device_info(root)
        suite_version = self._get_suite_version(root)      # 套件版本（如 16.1_r2）
        android_version = self._get_android_version(root)  # Android版本（build_version_release）
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
            suite_version=suite_version,
            android_version=android_version,
            start_time=start_time,
            total=total,
            pass_count=pass_count,
            fail_count=fail_count,
            pass_rate=pass_rate,
            failures=failures
        )

    def _get_test_type(self, root) -> str:
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

    def _get_device_info(self, root) -> str:
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

    def _get_suite_version(self, root) -> str:
        """获取测试套件版本（suite_version，如 16.1_r2）"""
        # 优先从Result根节点获取suite_version
        for attr in ['suite_version', 'version']:
            if root.get(attr):
                return root.get(attr)

        # 从Build节点获取
        build = root.find('.//Build')
        if build is not None:
            return build.get('suite_version', build.get('version', ''))

        return ''

    def _get_android_version(self, root) -> str:
        """获取Android版本（build_version_release）"""
        # 优先从Result根节点获取build_version_release
        for attr in ['build_version_release', 'android_version', 'AndroidVersion']:
            if root.get(attr):
                return root.get(attr)

        # 从Build节点获取build_version_release
        build = root.find('.//Build')
        if build is not None:
            return build.get('build_version_release', '')

        return ''

    def _get_start_time(self, root) -> str:
        """获取开始时间"""
        for attr in ['start_display', 'end_display', 'start_time', 'StartTime']:
            if root.get(attr):
                return root.get(attr)
        return '未知时间'

    def _get_summary(self, root) -> Tuple[int, int, int]:
        """获取摘要统计信息"""
        summary = root.find('.//Summary')
        if summary is not None:
            passed = int(summary.get('pass', summary.get('Passed', 0)))
            failed = int(summary.get('failed', summary.get('Failed', 0)))
            total = passed + failed
            return total, passed, failed

        # 如果没有Summary，手动统计
        return self._count_tests(root)

    def _count_tests(self, root) -> Tuple[int, int, int]:
        """手动统计测试用例"""
        total = 0
        passed = 0
        failed = 0
        for tc in root.iter('Test'):
            total += 1
            result = tc.get('result', 'pass').lower()
            if result == 'pass':
                passed += 1
            elif result == 'fail':
                failed += 1
        return total, passed, failed

    def _parse_failures(self, root) -> List[TestFailure]:
        """解析失败的测试用例"""
        failures = []

        for test_case in root.iter('Test'):
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

    def _get_parent(self, element):
        if USE_LXML:
            return element.getparent()
        if self.parent_map:
            return self.parent_map.get(element)
        return None

    def _get_module_name(self, test_case) -> str:
        """获取测试所属模块"""
        current = test_case
        while current is not None:
            current = self._get_parent(current)
            if current is None:
                break
            if current.tag == 'Module':
                return current.get('name', '未知模块')
        return '未知模块'

    def _get_test_name(self, test_case) -> str:
        """获取测试用例完整名称"""
        test_name = test_case.get('name', '未知用例')

        # 如果是Test节点，尝试组合完整名称
        if test_case.tag == 'Test':
            parent = self._get_parent(test_case)
            if parent is not None and parent.tag == 'TestCase':
                class_name = parent.get('name', '')
                if class_name and test_name:
                    return f"{class_name}#{test_name}"

        return test_name

    def _get_failure_info(self, test_case) -> Tuple[str, str]:
        """获取失败信息"""
        reason = ''
        stack_trace = ''

        failure = test_case.find('Failure')
        if failure is None:
            failure = test_case.find('.//Failure')
        if failure is not None:
            reason = failure.get('message', '')
            if failure.text:
                stack_trace = failure.text.strip()

        if not reason:
            error = test_case.find('Error')
            if error is None:
                error = test_case.find('.//Error')
            if error is not None:
                reason = error.get('message', '')
                if error.text:
                    stack_trace = error.text.strip()

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

    def parse_content(self, log_content: str, log_dir: str = '') -> Optional[TestReport]:
        """解析已读取的host_log内容。"""
        try:
            return self._parse_log_content(log_content, log_dir)
        except Exception as e:
            logger.error(f"HostLog内容解析失败: {e}")
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
        suite_version = "Unknown"

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
            suite_version=suite_version,
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

        return ''  # 统一返回空字符串

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
        self.report = None

    def analyze_file(self, file_path: str) -> Optional[Dict]:
        """分析报告文件"""
        # 确保临时目录存在
        os.makedirs(self.temp_dir, exist_ok=True)

        report = None

        lower_path = file_path.lower()

        if lower_path.endswith(('.zip', '.tar.gz', '.tgz', '.tar')):
            report = self._analyze_archive(file_path)
        elif lower_path.endswith('.xml'):
            report = self.parser.parse_file(file_path)
        else:
            logger.error(f"不支持的文件格式: {file_path}")
            return None

        if report:
            self.report = report
            return self._report_to_dict(report)
        return None

    @staticmethod
    def _archive_basename(member_name: str) -> str:
        return os.path.basename(member_name.replace('\\', '/'))

    @classmethod
    def _is_test_result_member(cls, member_name: str) -> bool:
        return cls._archive_basename(member_name) == 'test_result.xml'

    @classmethod
    def _is_host_log_member(cls, member_name: str) -> bool:
        basename = cls._archive_basename(member_name)
        return basename.startswith('host_log_') and basename.endswith('.txt')

    def _parse_host_log_stream(self, stream, member_name: str) -> Optional[TestReport]:
        with io.TextIOWrapper(stream, encoding='utf-8', errors='ignore') as text_stream:
            return self.host_log_parser.parse_content(
                text_stream.read(),
                os.path.dirname(member_name.replace('\\', '/'))
            )

    def _analyze_archive(self, archive_path: str) -> Optional[TestReport]:
        """直接从压缩包中读取目标文件，避免完整解压大报告。"""
        lower_path = archive_path.lower()
        try:
            if lower_path.endswith('.zip'):
                return self._analyze_zip_archive(archive_path)
            return self._analyze_tar_archive(archive_path)
        except Exception as e:
            logger.error(f"压缩包分析失败: {e}")
            return None

    def _analyze_zip_archive(self, archive_path: str) -> Optional[TestReport]:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            file_infos = [info for info in zf.infolist() if not info.is_dir()]

            xml_info = next((info for info in file_infos if self._is_test_result_member(info.filename)), None)
            if xml_info:
                with zf.open(xml_info) as stream:
                    return self.parser.parse_stream(stream)

            host_log_info = next((info for info in file_infos if self._is_host_log_member(info.filename)), None)
            if host_log_info:
                with zf.open(host_log_info) as stream:
                    return self._parse_host_log_stream(stream, host_log_info.filename)

        return None

    def _analyze_tar_archive(self, archive_path: str) -> Optional[TestReport]:
        with tarfile.open(archive_path, 'r:*') as tf:
            file_members = [member for member in tf.getmembers() if member.isfile()]

            xml_member = next((member for member in file_members if self._is_test_result_member(member.name)), None)
            if xml_member:
                stream = tf.extractfile(xml_member)
                if stream:
                    with stream:
                        return self.parser.parse_stream(stream)

            host_log_member = next((member for member in file_members if self._is_host_log_member(member.name)), None)
            if host_log_member:
                stream = tf.extractfile(host_log_member)
                if stream:
                    with stream:
                        return self._parse_host_log_stream(stream, host_log_member.name)

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

    def rk_codesearch(self, class_name: str, failure_location: dict = None, max_results: int = 5) -> List[Dict[str, str]]:
        """
        Args:
            class_name: 类名 (如 com.android.cts.permission.PermissionTest)
            failure_location: 从堆栈提取的失败位置 {file_name, file_type, line_number}
            max_results: 最大返回结果数

        Returns:
            List[Dict]: 搜索结果列表，每个包含 {project, path, line, type, file_type}
        """
        import subprocess

        web_app_dir = Path(__file__).resolve().parents[1]
        codesearch_dir = web_app_dir / 'skills' / 'rk_codesearch'
        codesearch_script = str(codesearch_dir / 'run.py')

        try:
            # 如果有精确失败位置，优先使用
            if failure_location:
                file_name = failure_location.get('file_name', '')
                file_type = failure_location.get('file_type', '')
                line_number = failure_location.get('line_number', '')

                # 直接使用文件名搜索（更精确）
                simple_name = file_name.split('$')[0]  # 去除内部类后缀

                cmd = [
                    'python3',
                    codesearch_script,
                    'search',
                    '--keywords', simple_name,
                    '--search-field', 'path',
                    '--limit', '10'
                ]

                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=str(codesearch_dir)
                    )
                except subprocess.TimeoutExpired:
                    logger.warning("代码搜索超时（30秒）")
                    return []
                except Exception as e:
                    logger.error(f"代码搜索异常: {e}")
                    return []

                if result.returncode != 0:
                    return []

                # 解析输出，找到匹配的文件
                search_results = []
                lines = result.stdout.strip().split('\n')

                target_file = f"{simple_name}.{file_type}"

                for i, line in enumerate(lines):
                    line = line.strip()
                    if not line:
                        continue

                    # 查找包含目标文件的路径
                    if target_file in line or (simple_name in line and f".{file_type}" in line):
                        # 提取项目信息
                        project = ''
                        for j in range(i + 1, min(len(lines), i + 3)):
                            if lines[j].strip().startswith('project:'):
                                project = lines[j].strip().split(':', 1)[1].strip()
                                break

                        search_results.append({
                            'type': 'definition',
                            'path': line.replace('[definition] ', '').strip() if line.startswith('[definition]') else line,
                            'line': line_number,
                            'file_type': file_type,
                            'project': project,
                            'is_exact_location': True
                        })
                        break

                if search_results:
                    return search_results[:max_results]

            # 没有失败位置时，使用类名搜索定义
            simple_class_name = class_name.split('.')[-1]

            cmd = [
                'python3',
                codesearch_script,
                'search',
                '--keywords', simple_class_name,
                '--search-field', 'def',
                '--limit', str(max_results)
            ]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(codesearch_dir)
                )
            except subprocess.TimeoutExpired:
                logger.warning("代码搜索超时（30秒）")
                return []
            except Exception as e:
                logger.error(f"代码搜索异常: {e}")
                return []

            if result.returncode != 0:
                return []

            # 预加载OpenGrok配置（避免在循环中重复加载）
            opengrok_config = {}
            try:
                from core.config import config_manager
                opengrok_config = config_manager.load_config().get('opengrok', {})
            except Exception:
                pass

            # 根据Android版本动态选择OpenGrok项目
            selected_project = get_opengrok_project_for_android_version(
                self.report.android_version if self.report else '', opengrok_config
            )

            # 解析输出
            search_results = []
            lines = result.stdout.strip().split('\n')

            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line:
                    i += 1
                    continue

                if line.startswith('[') and ']' in line:
                    bracket_content = line[1:line.index(']')]
                    rest_of_line = line[line.index(']')+1:].strip()

                    result_item = {
                        'type': bracket_content,
                        'path': rest_of_line,
                        'file_type': 'kt' if rest_of_line.endswith('.kt') else 'java'
                    }

                    # 生成OpenGrok URL（使用动态选择的项目）
                    if opengrok_config.get('base_url') and selected_project:
                        base_url = opengrok_config['base_url']
                        project = selected_project
                        result_item['_opengrok_base_url'] = base_url
                        result_item['_opengrok_project'] = project

                    for j in range(i + 1, min(len(lines), i + 3)):
                        next_line = lines[j].strip()
                        if next_line.startswith('project:'):
                            result_item['project'] = next_line.split(':', 1)[1].strip()
                            break
                        elif next_line.startswith('['):
                            break

                    for j in range(i + 1, min(len(lines), i + 4)):
                        next_line = lines[j].strip()
                        if next_line and not next_line.startswith('project:') and not next_line.startswith('['):
                            if ':' in next_line:
                                line_num_part = next_line.split(':')[0].strip()
                                if line_num_part.isdigit():
                                    result_item['line'] = line_num_part
                                    # 生成完整的OpenGrok URL
                                    if '_opengrok_base_url' in result_item and '_opengrok_project' in result_item:
                                        result_item['url'] = f"{result_item['_opengrok_base_url']}/xref/{result_item['_opengrok_project']}/{result_item['path']}#{result_item['line']}"
                                        # 清理临时字段
                                        del result_item['_opengrok_base_url']
                                        del result_item['_opengrok_project']
                                    break

                    # 如果没有找到line号，但有OpenGrok配置，也生成URL（没有#行号）
                    if 'line' not in result_item and '_opengrok_base_url' in result_item:
                        result_item['url'] = f"{result_item['_opengrok_base_url']}/xref/{result_item['_opengrok_project']}/{result_item['path']}"
                        del result_item['_opengrok_base_url']
                        del result_item['_opengrok_project']

                    search_results.append(result_item)

                i += 1

            # 去重：按路径去重
            seen_paths = set()
            unique_results = []
            for item in search_results:
                if item['path'] not in seen_paths:
                    seen_paths.add(item['path'])
                    unique_results.append(item)

            return unique_results[:max_results]

        except subprocess.TimeoutExpired:
            logger.warning("代码搜索超时")
            return []
        except Exception as e:
            logger.error(f"代码搜索异常：{e}")
            return []
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
                'suite_version': report.suite_version,        # 套件版本（如 16.1_r2）
                'android_version': report.android_version,    # Android版本（build_version_release）
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
