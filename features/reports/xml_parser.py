from __future__ import annotations

import logging

from .models import _LXML_PARSER, ET, USE_LXML, TestFailure, TestReport, etree


logger = logging.getLogger(__name__)

class XMLReportParser:
    """XML报告解析器 - 统一处理test_result.xml解析"""

    def __init__(self):
        self.parent_map = None

    def parse_file(self, xml_path: str) -> TestReport | None:
        """解析XML文件"""
        try:
            if USE_LXML:
                tree = etree.parse(xml_path, _LXML_PARSER)
                root = tree.getroot()
            else:
                tree = ET.parse(xml_path)
                root = tree.getroot()

            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML解析失败: {e}")
            return None

    def parse_stream(self, xml_stream) -> TestReport | None:
        """从文件流解析XML，避免为压缩包先解压落盘。"""
        try:
            if USE_LXML:
                tree = etree.parse(xml_stream, _LXML_PARSER)
                root = tree.getroot()
            else:
                tree = ET.parse(xml_stream)
                root = tree.getroot()

            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML流解析失败: {e}")
            return None

    def parse_content(self, xml_content: str) -> TestReport | None:
        """解析XML内容字符串"""
        try:
            if USE_LXML:
                root = etree.fromstring(xml_content.encode('utf-8'), _LXML_PARSER)
            else:
                root = ET.fromstring(xml_content)

            return self._parse_root(root)
        except Exception as e:
            logger.error(f"XML内容解析失败: {e}")
            return None

    def _parse_root(self, root) -> TestReport | None:
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

    @staticmethod
    def _first_attr(element, *names: str) -> str:
        """Return the first non-empty attribute value from *names*, or ''."""
        for name in names:
            val = element.get(name)
            if val:
                return val
        return ''

    def _get_test_type(self, root) -> str:
        """获取测试类型"""
        val = self._first_attr(root, 'suite_name', 'suite', 'test_type', 'testType', 'type', 'Type')
        if val:
            return val
        build = root.find('.//Build')
        if build is not None:
            return self._first_attr(build, 'test_type', 'testType') or 'GTS'
        return 'GTS'

    def _get_device_info(self, root) -> str:
        """获取设备信息"""
        val = self._first_attr(root, 'devices')
        if val:
            return val
        build = root.find('.//Build')
        if build is not None:
            return self._first_attr(build, 'device_serial', 'serial')
        return '未知设备'

    def _get_suite_version(self, root) -> str:
        """获取测试套件版本（suite_version，如 16.1_r2）"""
        val = self._first_attr(root, 'suite_version', 'version')
        if val:
            return val
        build = root.find('.//Build')
        if build is not None:
            return self._first_attr(build, 'suite_version', 'version')
        return ''

    def _get_android_version(self, root) -> str:
        """获取Android版本（build_version_release）"""
        val = self._first_attr(root, 'build_version_release', 'android_version', 'AndroidVersion')
        if val:
            return val
        build = root.find('.//Build')
        if build is not None:
            return self._first_attr(build, 'build_version_release')
        return ''

    def _get_start_time(self, root) -> str:
        """获取开始时间"""
        return self._first_attr(root, 'start_display', 'end_display', 'start_time', 'StartTime') or '未知时间'

    def _get_summary(self, root) -> tuple[int, int, int]:
        """获取摘要统计信息"""
        summary = root.find('.//Summary')
        if summary is not None:
            passed = int(summary.get('pass', summary.get('Passed', 0)))
            failed = int(summary.get('failed', summary.get('Failed', 0)))
            total = passed + failed
            return total, passed, failed

        # 如果没有Summary，手动统计
        return self._count_tests(root)

    def _count_tests(self, root) -> tuple[int, int, int]:
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

    def _parse_failures(self, root) -> list[TestFailure]:
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

    def _get_failure_info(self, test_case) -> tuple[str, str]:
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


