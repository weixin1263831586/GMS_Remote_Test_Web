from __future__ import annotations

import logging
import os
import re

from .models import TestFailure, TestReport


logger = logging.getLogger(__name__)

class HostLogParser:
    """HostLog解析器 - 统一处理CTS/VTS/GTS等测试套件的host_log分析"""

    def parse_log_dir(self, log_dir: str) -> TestReport | None:
        """解析日志目录"""
        try:
            # 查找host_log文件
            host_log_path = self._find_host_log(log_dir)
            if not host_log_path:
                return None

            with open(host_log_path, encoding='utf-8', errors='ignore') as f:
                log_content = f.read()

            return self._parse_log_content(log_content, log_dir)
        except Exception as e:
            logger.error(f"HostLog解析失败: {e}")
            return None

    def parse_content(self, log_content: str, log_dir: str = '') -> TestReport | None:
        """解析已读取的host_log内容。"""
        try:
            return self._parse_log_content(log_content, log_dir)
        except Exception as e:
            logger.error(f"HostLog内容解析失败: {e}")
            return None

    # Known host-log filename prefixes ordered by priority (lower = higher priority).
    _HOST_LOG_PREFIXES = (
        'host_log_',
        'invoc_complete_host_log_',
        'end_host_log_',
        'host_log',
    )

    @classmethod
    def _is_host_log_filename(cls, filename: str) -> bool:
        """Check whether a filename matches any known host-log convention."""
        lower = filename.lower()
        return lower.endswith('.txt') and any(lower.startswith(p) for p in cls._HOST_LOG_PREFIXES)

    def _find_host_log(self, log_dir: str) -> str | None:
        """查找host_log文件"""
        candidates = []
        for root, _dirs, files in os.walk(log_dir):
            for file in files:
                if self._is_host_log_filename(file):
                    candidates.append(os.path.join(root, file))
        if not candidates:
            return None

        def priority(path: str) -> tuple:
            name = os.path.basename(path).lower()
            for idx, prefix in enumerate(self._HOST_LOG_PREFIXES):
                if name.startswith(prefix):
                    return (idx, name)
            return (len(self._HOST_LOG_PREFIXES), name)

        return sorted(candidates, key=priority)[0]

    def _parse_log_content(self, log_content: str, log_dir: str) -> TestReport | None:
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
        dir_name = os.path.basename(log_dir).lower()
        for suite in ('CTS', 'VTS', 'GTS'):
            if suite.lower() in dir_name:
                return suite
        for suite in ('VTS', 'CTS', 'GTS'):
            if suite in log_content or suite.lower() in log_content:
                return suite
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

        match = re.search(r'\b([A-Za-z0-9_]+)\s+running\s+\d+\s+modules?:', log_content)
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

    def _extract_failures(self, log_content: str) -> list[TestFailure]:
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
                module_to_use = detailed_module or current_module

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

    def _generate_summary(self, log_content: str) -> tuple[int, int, int]:
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

