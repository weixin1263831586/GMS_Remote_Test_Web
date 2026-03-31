"""
使用Claude API分析GMS测试报告
支持自动解析日志、XML并提供智能洞察
"""

import os
import re
import logging
import json
from typing import Dict, List, Optional
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CTSSummary:
    """CTS测试摘要数据"""
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    duration: str = ""
    retry_success: int = 0
    retry_failure: int = 0
    log_dir: str = ""
    result_dir: str = ""
    test_module: str = ""
    test_case: str = ""
    device: str = ""


class CTSSummaryParser:
    """CTS日志摘要解析器"""

    @staticmethod
    def parse_from_log(log_content: str) -> CTSSummary:
        """从日志内容中解析CTS摘要"""
        summary = CTSSummary()

        lines = log_content.split('\n')
        in_summary_block = False

        for line in lines:
            # 检测摘要块
            if '=== Summary ===' in line or '=== Results ===' in line:
                in_summary_block = True
                continue

            if '=== End of Results ===' in line or '====================' in line:
                if in_summary_block:
                    break

            if not in_summary_block:
                # 在摘要块之前，提取测试信息
                if '测试用例:' in line or 'Test case:' in line:
                    summary.test_case = line.split(':', 1)[1].strip()
                elif '测试设备:' in line or 'Device:' in line:
                    summary.device = line.split(':', 1)[1].strip().replace('-s ', '')
                continue

            # 解析摘要内容
            if 'Total Tests' in line and ':' in line:
                summary.total_tests = CTSSummaryParser._extract_number(line)
            elif 'PASSED' in line and ':' in line:
                summary.passed = CTSSummaryParser._extract_number(line)
            elif 'FAILED' in line and ':' in line:
                summary.failed = CTSSummaryParser._extract_number(line)
            elif 'Total Run time' in line and ':' in line:
                summary.duration = line.split(':', 1)[1].strip()
            elif 'LOG DIRECTORY' in line and ':' in line:
                summary.log_dir = line.split(':', 1)[1].strip()
            elif 'RESULT DIRECTORY' in line and ':' in line:
                summary.result_dir = line.split(':', 1)[1].strip()
            elif 'Retry Success' in line and '=' in line:
                summary.retry_success = CTSSummaryParser._extract_number(line.split('=')[1])
            elif 'Retry Failure' in line and '=' in line:
                summary.retry_failure = CTSSummaryParser._extract_number(line.split('=')[1])

        return summary

    @staticmethod
    def _extract_number(text: str) -> int:
        """从文本中提取数字"""
        match = re.search(r'\d+', text)
        return int(match.group()) if match else 0


class ClaudeReportAnalyzer:
    """Claude报告分析器"""

    def __init__(self):
        self.summary_parser = CTSSummaryParser()

    def analyze_report_file(self, result_dir: str, log_file: str = None) -> Dict:
        """
        分析测试报告

        Args:
            result_dir: 结果目录路径
            log_file: 日志文件路径（可选）

        Returns:
            dict: 分析结果
        """
        try:
            # 1. 读取日志文件
            log_content = ""
            if log_file and os.path.exists(log_file):
                with open(log_file, 'r', encoding='utf-8') as f:
                    log_content = f.read()
            elif result_dir:
                # 尝试从result_dir查找日志
                log_pattern = os.path.join(os.path.dirname(result_dir), 'logs', '*.log')
                import glob
                log_files = glob.glob(log_pattern)
                if log_files:
                    with open(log_files[-1], 'r', encoding='utf-8') as f:
                        log_content = f.read()

            # 2. 解析CTS摘要
            summary = self.summary_parser.parse_from_log(log_content) if log_content else CTSSummary()

            # 3. 生成结构化分析
            analysis = {
                'success': True,
                'summary': {
                    'status': 'PASSED' if summary.failed == 0 else 'FAILED',
                    'total_tests': summary.total_tests,
                    'passed': summary.passed,
                    'failed': summary.failed,
                    'pass_rate': f"{(summary.passed / summary.total_tests * 100):.1f}%" if summary.total_tests > 0 else "0%",
                    'duration': summary.duration,
                    'retry_success': summary.retry_success,
                    'retry_failure': summary.retry_failure,
                },
                'test_info': {
                    'module': summary.test_module or 'Unknown',
                    'test_case': summary.test_case or 'Unknown',
                    'device': summary.device or 'Unknown',
                },
                'paths': {
                    'log_dir': summary.log_dir,
                    'result_dir': summary.result_dir,
                },
                'insights': self._generate_insights(summary),
                'raw_log_summary': self._extract_relevant_logs(log_content),
            }

            return analysis

        except Exception as e:
            logger.error(f"分析报告失败: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _generate_insights(self, summary: CTSSummary) -> List[Dict]:
        """生成测试洞察"""
        insights = []

        # 失败分析
        if summary.failed > 0:
            if summary.retry_failure > 0:
                insights.append({
                    'type': 'error',
                    'icon': '❌',
                    'title': '测试失败且重试未通过',
                    'message': f'{summary.retry_failure} 个测试用例在重试后仍然失败，需要检查设备状态、测试环境或代码逻辑'
                })
            else:
                insights.append({
                    'type': 'warning',
                    'icon': '⚠️',
                    'title': '测试失败',
                    'message': f'{summary.failed} 个测试用例失败，建议查看详细日志分析失败原因'
                })

        # 重试成功分析
        if summary.retry_success > 0:
            insights.append({
                'type': 'info',
                'icon': '🔄',
                'title': '重试成功',
                'message': f'{summary.retry_success} 个测试用例在重试后通过，可能是间歇性不稳定问题'
            })

        # 性能分析
        if summary.duration:
            minutes_match = re.search(r'(\d+)m', summary.duration)
            if minutes_match:
                minutes = int(minutes_match.group(1))
                if minutes > 10:
                    insights.append({
                        'type': 'info',
                        'icon': '⏱️',
                        'title': '测试耗时较长',
                        'message': f'总耗时 {summary.duration}，建议检查是否有性能瓶颈或网络延迟'
                    })

        # 成功提示
        if summary.failed == 0 and summary.total_tests > 0:
            insights.append({
                'type': 'success',
                'icon': '✅',
                'title': '全部通过',
                'message': f'所有 {summary.total_tests} 个测试用例均通过，测试执行成功'
            })

        return insights

    def _extract_relevant_logs(self, log_content: str, max_lines: int = 50) -> List[str]:
        """提取相关日志行"""
        if not log_content:
            return []

        relevant_lines = []
        keywords = ['FAILED', 'PASSED', 'Error', 'Exception', '===', 'Summary']

        for line in log_content.split('\n'):
            line = line.strip()
            if any(keyword in line for keyword in keywords):
                relevant_lines.append(line)
                if len(relevant_lines) >= max_lines:
                    break

        return relevant_lines[-max_lines:]  # 返回最后max_lines行

    def analyze_with_claude_api(self, report_data: Dict, api_key: str = None) -> Dict:
        """
        使用Claude API进行深度分析

        Args:
            report_data: 报告数据（来自analyze_report_file）
            api_key: Claude API密钥（可选，如果提供则使用Claude API）

        Returns:
            dict: Claude分析结果
        """
        try:
            import anthropic

            if not api_key:
                return {
                    'success': False,
                    'error': 'Claude API密钥未提供'
                }

            # 构造提示词
            prompt = self._build_claude_prompt(report_data)

            # 调用Claude API
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=2000,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            analysis_text = response.content[0].text

            # 解析Claude响应
            return {
                'success': True,
                'analysis': analysis_text,
                'suggestions': self._extract_suggestions(analysis_text)
            }

        except ImportError:
            return {
                'success': False,
                'error': 'anthropic包未安装，请运行: pip install anthropic'
            }
        except Exception as e:
            logger.error(f"Claude API分析失败: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _build_claude_prompt(self, report_data: Dict) -> str:
        """构造Claude提示词"""
        prompt = f"""你是一个专业的Android CTS测试分析专家。请分析以下测试报告：

## 测试概要
- 状态: {report_data['summary']['status']}
- 总测试数: {report_data['summary']['total_tests']}
- 通过: {report_data['summary']['passed']}
- 失败: {report_data['summary']['failed']}
- 通过率: {report_data['summary']['pass_rate']}
- 耗时: {report_data['summary']['duration']}
- 重试成功: {report_data['summary']['retry_success']}
- 重试失败: {report_data['summary']['retry_failure']}

## 测试信息
- 模块: {report_data['test_info']['module']}
- 测试用例: {report_data['test_info']['test_case']}
- 设备: {report_data['test_info']['device']}

## 系统洞察
"""

        for insight in report_data.get('insights', []):
            prompt += f"\n- {insight['icon']} {insight['title']}: {insight['message']}"

        if report_data.get('raw_log_summary'):
            prompt += f"\n\n## 关键日志\n```\n"
            prompt += '\n'.join(report_data['raw_log_summary'][-20:])  # 最后20行
            prompt += "\n```"

        prompt += """

请提供以下分析（使用Markdown格式）：

1. **问题诊断**: 测试失败的根本原因是什么？
2. **风险评估**: 这个失败对系统的影响程度？
3. **修复建议**: 具体的修复步骤和验证方法
4. **预防措施**: 如何避免类似问题再次发生

请使用专业但易懂的语言，给出可执行的建议。
"""

        return prompt

    def _extract_suggestions(self, analysis_text: str) -> List[str]:
        """从分析文本中提取建议"""
        suggestions = []

        # 查找建议部分
        lines = analysis_text.split('\n')
        in_suggestions = False

        for line in lines:
            if '修复建议' in line or '建议' in line:
                in_suggestions = True
                continue

            if in_suggestions:
                if line.strip().startswith('-') or line.strip().startswith('•') or re.match(r'^\d+\.', line):
                    suggestions.append(line.strip())
                elif not line.strip():
                    break

        return suggestions[:5]  # 最多返回5条建议
