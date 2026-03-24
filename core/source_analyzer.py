"""
Android源码分析器
使用OpenGrok获取Android源码并分析失败原因，给出修改建议
"""

import re
import requests
import logging
import json
import os
import subprocess
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

logger = logging.getLogger(__name__)


class AndroidSourceAnalyzer:
    """Android源码分析器 (使用OpenGrok)"""

    def __init__(self):
        self.opengrok_plugin = "/home/hcq/remote-run-server/plugins/commands/opengrok/run.py"
        self.timeout = 15

    def fetch_source_code(self, class_name: str, package: Optional[str] = None) -> Optional[Dict]:
        """
        使用OpenGrok获取Android源码

        Args:
            class_name: 类名（如 AngleAllowlistTraceTest）
            package: 包名（如 com.google.android.angleallowlists.vts）

        Returns:
            dict: 包含源码信息，如果失败返回None
        """
        try:
            # 构造文件名
            simple_class_name = class_name.split('.')[-1]
            filename = f"{simple_class_name}.java"

            logger.info(f"使用OpenGrok获取源码: {simple_class_name}")

            # 使用OpenGrok搜索源码
            result = self._fetch_via_opengrok(simple_class_name)

            if result and result.get('content'):
                logger.info(f"通过OpenGrok成功获取源码: {result.get('file_path')}")
                return {
                    'class_name': simple_class_name,
                    'file_path': result.get('file_path', ''),
                    'source_url': result.get('source_url', ''),
                    'content': result['content'],
                    'filename': filename,
                    'fetch_method': 'opengrok'
                }

            logger.warning(f"OpenGrok无法获取源码: {class_name}")
            return None

        except Exception as e:
            logger.error(f"获取源码失败: {e}")
            return None

    def _fetch_via_opengrok(self, class_name: str) -> Optional[Dict]:
        """
        使用OpenGrok插件搜索并获取源码

        Args:
            class_name: 类名

        Returns:
            dict: 包含源码内容和路径信息
        """
        try:
            if not os.path.exists(self.opengrok_plugin):
                logger.warning(f"OpenGrok插件不存在: {self.opengrok_plugin}")
                return None

            # 调用OpenGrok插件搜索
            cmd = [
                'python3',
                self.opengrok_plugin,
                'search',
                '--query', class_name,
                '--search-field', 'full',
                '--limit', '3'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )

            if result.returncode != 0:
                logger.debug(f"OpenGrok搜索失败: {result.stderr}")
                return None

            # 解析搜索结果 - 从文本输出中提取文件路径
            lines = result.stdout.strip().split('\n')
            file_path = None
            project = None

            # 查找包含 [reference], [path], [definition] 等标记的行
            for i, line in enumerate(lines):
                line = line.strip()
                # 匹配格式: [type] project/path
                if line.startswith('[') and ']' in line:
                    # 提取路径部分
                    # 例如: [reference] cts/libs/input/src/com/android/cts/input/UinputTouchDevice.kt
                    after_bracket = line.split(']', 1)[1].strip()
                    if after_bracket:
                        # 检查下一行是否有 project 信息
                        if i + 1 < len(lines):
                            next_line = lines[i + 1].strip()
                            if next_line.startswith('project:'):
                                # 提取项目名
                                project = next_line.split(':', 1)[1].strip()
                                # 构造完整路径: project/path
                                file_path = f"{project}/{after_bracket}"
                                break

                        # 如果没有 project 行,直接使用路径
                        if '/' in after_bracket and any(after_bracket.endswith(ext) for ext in ['.java', '.kt', '.cpp', '.c', '.h']):
                            file_path = after_bracket
                            break

            if not file_path:
                logger.debug(f"OpenGrok未找到结果: {class_name}")
                logger.debug(f"输出: {result.stdout[:500]}")
                return None

            # 获取文件内容
            content = self._fetch_file_content_from_opengrok(file_path)

            if content:
                return {
                    'file_path': file_path,
                    'source_url': self._get_opengrok_url(file_path),
                    'content': content
                }

            return None

        except subprocess.TimeoutExpired:
            logger.warning(f"OpenGrok搜索超时: {class_name}")
            return None
        except Exception as e:
            logger.debug(f"OpenGrok搜索异常: {e}")
            return None

    def _fetch_file_content_from_opengrok(self, file_path: str) -> Optional[str]:
        """
        从OpenGrok API获取文件内容

        Args:
            file_path: 文件路径

        Returns:
            str: 文件内容
        """
        try:
            # 读取配置
            config_path = os.path.join(os.path.dirname(self.opengrok_plugin), 'config/config.json')
            base_url = "http://10.10.10.203:8080/source"
            token = "G3wtcawHUYvsv1whz"

            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    base_url = config.get('base_url', base_url)
                    token = config.get('token', token)

            # 构造API URL
            # 格式: /api/v1/files/{project}/path/{encoded_path}
            project = "Android14"  # 默认项目,可以从配置中读取

            # 分离项目名和路径
            path_parts = file_path.split('/', 1)
            if len(path_parts) > 1 and path_parts[0] in ['Android14', 'Android13', 'Android16', 'Android12', 'Android11']:
                project = path_parts[0]
                file_path = path_parts[1]

            encoded_path = file_path
            api_url = f"{base_url}/api/v1/files/{project}/path/{encoded_path}"

            # 发送请求
            response = requests.get(
                api_url,
                headers={'Authorization': f'Bearer {token}', 'Accept': 'text/plain'},
                timeout=self.timeout
            )

            if response.status_code == 200:
                return response.text

            logger.debug(f"获取文件内容失败: HTTP {response.status_code}")
            return None

        except Exception as e:
            logger.debug(f"获取文件内容失败: {e}")
            return None

    def _get_opengrok_url(self, file_path: str) -> str:
        """
        构造OpenGrok的URL

        Args:
            file_path: 文件路径

        Returns:
            str: OpenGrok URL
        """
        try:
            # 读取OpenGrok配置获取base_url
            config_path = os.path.join(os.path.dirname(self.opengrok_plugin), 'config/config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    base_url = config.get('base_url', 'http://10.10.10.203:8080/source')
                    return f"{base_url.rstrip('/')}/{file_path}"
        except:
            pass

        # 默认URL
        return f"http://10.10.10.203:8080/source/{file_path}"

    def analyze_failure_with_source(
        self,
        class_name: str,
        method_name: Optional[str],
        error_message: str,
        stack_trace: Optional[str] = None
    ) -> Dict:
        """
        结合源码分析失败原因并给出建议

        Args:
            class_name: 类名
            method_name: 方法名
            error_message: 错误信息
            stack_trace: 堆栈跟踪

        Returns:
            dict: 分析结果
        """
        result = {
            'source_found': False,
            'analysis': [],
            'suggestions': [],
            'relevant_code': [],
            'solution': None  # 具体解决方案
        }

        try:
            # 使用OpenGrok获取源码
            source_info = self.fetch_source_code(class_name)

            if not source_info or not source_info.get('content'):
                logger.info("无法从OpenGrok获取源码，使用错误模式分析")
                result['analysis'].append("无法自动获取源码，基于错误模式进行分析")
                # 即使没有源码，也要提供有价值的分析
                pattern_analysis = self._analyze_by_error_pattern(error_message, stack_trace)
                result['analysis'].extend(pattern_analysis['analysis'])
                result['suggestions'].extend(pattern_analysis['suggestions'])
                result['solution'] = pattern_analysis.get('solution')
                return result

            # 验证源码内容
            source_code = source_info['content']
            if not source_code or len(source_code.strip()) < 50:
                result['analysis'].append("从OpenGrok获取的源码内容不完整")
                pattern_analysis = self._analyze_by_error_pattern(error_message, stack_trace)
                result['analysis'].extend(pattern_analysis['analysis'])
                result['suggestions'].extend(pattern_analysis['suggestions'])
                result['solution'] = pattern_analysis.get('solution')
                return result

            result['source_found'] = True
            result['source_url'] = source_info.get('source_url', '')
            result['file_path'] = source_info.get('file_path', class_name + '.java')

            # 深度分析
            error_type = self._extract_error_type(error_message)

            # 提取相关代码
            relevant_code = self._extract_relevant_code(
                source_code,
                method_name,
                error_message
            )

            # 如果没找到方法，显示文件预览
            if not relevant_code:
                lines = source_code.split('\n')
                preview_lines = lines[:min(50, len(lines))]
                relevant_code.append({
                    'type': 'file_preview',
                    'name': '文件开头',
                    'code': '\n'.join(preview_lines)
                })

            result['relevant_code'] = relevant_code

            # 结合源码和错误信息进行深度分析
            deep_analysis = self._deep_analyze_failure(
                source_code,
                error_type,
                error_message,
                stack_trace,
                method_name,
                relevant_code
            )

            result['analysis'].extend(deep_analysis['analysis'])
            result['suggestions'].extend(deep_analysis['suggestions'])
            result['solution'] = deep_analysis.get('solution')

        except Exception as e:
            logger.error(f"源码分析失败: {e}", exc_info=True)
            result['analysis'].append(f"分析过程出错: {str(e)}")

        return result

    def _deep_analyze_failure(
        self,
        source_code: str,
        error_type: Optional[str],
        error_message: str,
        stack_trace: Optional[str],
        method_name: Optional[str],
        relevant_code: List[Dict]
    ) -> Dict:
        """深度分析失败原因并提供解决方案"""
        analysis = []
        suggestions = []
        solution = None

        # 提取关键信息
        error_keywords = self._extract_keywords_from_error(error_message)
        failing_line = self._extract_failing_line(stack_trace)

        # 根据错误类型分析
        if error_type:
            if 'assertion' in error_type.lower():
                solution = self._analyze_assertion_failure(
                    source_code, error_message, method_name, relevant_code
                )
            elif 'nullpointer' in error_type.lower():
                solution = self._analyze_nullpointer(
                    source_code, error_message, relevant_code, failing_line
                )
            elif 'illegalstate' in error_type.lower():
                solution = self._analyze_illegal_state(
                    source_code, error_message, relevant_code
                )
            elif 'timeout' in error_type.lower() or 'timedout' in error_type.lower():
                solution = self._analyze_timeout(
                    source_code, method_name, relevant_code
                )

        # 如果没有具体解决方案，使用通用分析
        if not solution:
            solution = self._generate_generic_solution(
                error_type, error_message, error_keywords, method_name
            )

        analysis.append(solution.get('problem_description', ''))
        suggestions.extend(solution.get('suggestions', []))

        return {
            'analysis': analysis,
            'suggestions': suggestions,
            'solution': solution
        }

    def _analyze_assertion_failure(
        self,
        source_code: str,
        error_message: str,
        method_name: Optional[str],
        relevant_code: List[Dict]
    ) -> Dict:
        """分析断言失败"""
        # 提取断言信息
        assertion_match = re.search(r'expected[:\s]+<([^>]+)>\s*but was[:\s]+<([^>]+)>', error_message)

        problem = "测试断言失败，实际结果与预期不符"

        if assertion_match:
            expected = assertion_match.group(1)
            actual = assertion_match.group(2)
            problem += f"\n预期值: {expected}\n实际值: {actual}"

        suggestions = [
            "检查被测试功能的实现是否正确",
            "确认测试用例的预期值是否设置正确",
            "查看测试环境配置是否符合要求"
        ]

        # 查找断言语句
        for code in relevant_code:
            code_content = code.get('code', '')
            if 'assert' in code_content:
                suggestions.insert(0, "断言语句已在相关代码中标注，请检查断言条件")

        return {
            'problem_description': problem,
            'suggestions': suggestions,
            'error_type': 'AssertionError',
            'fix_strategy': 'verify_expectations'
        }

    def _analyze_nullpointer(
        self,
        source_code: str,
        error_message: str,
        relevant_code: List[Dict],
        failing_line: Optional[int]
    ) -> Dict:
        """分析空指针异常"""
        problem = "发生了空指针异常，某个对象在使用前未正确初始化"

        suggestions = [
            "在对象使用前添加null检查",
            "确保所有依赖项都正确注入或初始化",
            "检查测试环境的setup方法是否完整"
        ]

        # 尝试从源码中找到可能的对象初始化问题
        for code in relevant_code:
            code_content = code.get('code', '')
            # 查找可能的空指针风险
            if '=' in code_content and 'new ' not in code_content:
                lines = code_content.split('\n')
                for i, line in enumerate(lines):
                    if '=' in line and 'new' not in line and 'null' not in line:
                        # 可能的未初始化变量
                        var_match = re.search(r'(\w+)\s*=', line)
                        if var_match:
                            suggestions.insert(0, f"检查变量 '{var_match.group(1)}' 是否在使用前正确初始化")
                            break

        return {
            'problem_description': problem,
            'suggestions': suggestions,
            'error_type': 'NullPointerException',
            'fix_strategy': 'add_null_checks'
        }

    def _analyze_illegal_state(
        self,
        source_code: str,
        error_message: str,
        relevant_code: List[Dict]
    ) -> Dict:
        """分析非法状态异常"""
        problem = "当前操作不允许在当前状态下执行"

        # 提取具体的错误信息
        error_detail = re.search(r'IllegalStateException:\s*(.+)', error_message)
        if error_detail:
            problem += f"\n详情: {error_detail.group(1)}"

        suggestions = [
            "检查操作的执行顺序是否正确",
            "确保必要的前置条件已满足",
            "验证组件的生命周期状态"
        ]

        return {
            'problem_description': problem,
            'suggestions': suggestions,
            'error_type': 'IllegalStateException',
            'fix_strategy': 'verify_state'
        }

    def _analyze_timeout(
        self,
        source_code: str,
        method_name: Optional[str],
        relevant_code: List[Dict]
    ) -> Dict:
        """分析超时问题"""
        problem = "测试执行超时"

        suggestions = [
            "增加测试超时时间",
            "检查是否存在死锁或无限循环",
            "优化测试代码的执行效率",
            "确认被测试系统是否响应正常"
        ]

        # 检查是否有等待机制
        for code in relevant_code:
            if 'wait' in code.get('code', '').lower() or 'sleep' in code.get('code', '').lower():
                suggestions.insert(0, "测试中包含等待机制，检查等待时间是否合理")
                break

        return {
            'problem_description': problem,
            'suggestions': suggestions,
            'error_type': 'Timeout',
            'fix_strategy': 'adjust_timeout'
        }

    def _generate_generic_solution(
        self,
        error_type: Optional[str],
        error_message: str,
        keywords: List[str],
        method_name: Optional[str]
    ) -> Dict:
        """生成通用解决方案"""
        problem = f"检测到错误: {error_type if error_type else '未知错误'}"

        suggestions = []

        # 基于错误模式生成建议
        if 'not found' in error_message.lower():
            problem = "资源或依赖项未找到"
            suggestions = [
                "检查测试所需的资源是否存在",
                "确认依赖库是否正确引入",
                "验证文件路径配置是否正确"
            ]
        elif 'permission' in error_message.lower():
            problem = "权限不足"
            suggestions = [
                "检查应用权限配置",
                "确认测试环境的权限设置",
                "查看manifest文件中的权限声明"
            ]
        elif 'connection' in error_message.lower():
            problem = "连接失败"
            suggestions = [
                "检查网络连接是否正常",
                "确认服务端是否正在运行",
                "验证连接配置是否正确"
            ]
        else:
            suggestions = [
                "查看完整的错误堆栈跟踪",
                "检查测试环境配置",
                "参考相关文档或类似问题的解决方案"
            ]

        return {
            'problem_description': problem,
            'suggestions': suggestions,
            'error_type': error_type or 'Unknown',
            'fix_strategy': 'generic'
        }

    def _analyze_by_error_pattern(self, error_message: str, stack_trace: Optional[str]) -> Dict:
        """基于错误模式进行分析（无需源码）"""
        analysis = []
        suggestions = []
        solution = None

        error_type = self._extract_error_type(error_message)

        # 根据错误信息模式分析
        if 'assertion' in str(error_type).lower():
            analysis.append("这是一个断言失败，表明测试的实际结果与预期不符")
            suggestions = [
                "检查被测试功能的实现逻辑",
                "确认测试用例的预期值是否正确",
                "验证测试环境配置",
                "查看完整的堆栈跟踪定位失败位置"
            ]
            solution = {
                'problem_description': '断言失败：实际结果与预期不符',
                'suggestions': suggestions,
                'error_type': 'AssertionError',
                'fix_strategy': 'verify_expectations'
            }

        elif 'nullpointer' in str(error_type).lower():
            analysis.append("发生了空指针异常")
            suggestions = [
                "检查测试代码中所有对象是否正确初始化",
                "在关键位置添加空值检查",
                "确认测试环境的setUp方法是否完整",
                "检查依赖的组件或服务是否可用"
            ]
            solution = {
                'problem_description': '空指针异常：对象未初始化',
                'suggestions': suggestions,
                'error_type': 'NullPointerException',
                'fix_strategy': 'add_null_checks'
            }

        elif 'timeout' in error_message.lower() or 'timedout' in error_message.lower():
            analysis.append("测试执行超时")
            suggestions = [
                "增加测试的超时时间配置",
                "检查是否存在死锁或性能问题",
                "优化测试代码或被测试代码",
                "确认系统资源是否充足"
            ]
            solution = {
                'problem_description': '测试执行超时',
                'suggestions': suggestions,
                'error_type': 'Timeout',
                'fix_strategy': 'adjust_timeout'
            }

        else:
            analysis.append(f"检测到错误: {error_type if error_type else '未知错误'}")
            suggestions = [
                "查看完整的堆栈跟踪信息",
                "使用OpenGrok搜索相关源码",
                "检查测试环境配置",
                "参考错误信息中的关键字进行排查"
            ]
            solution = {
                'problem_description': f'错误类型: {error_type if error_type else "未知"}',
                'suggestions': suggestions,
                'error_type': error_type or 'Unknown',
                'fix_strategy': 'generic'
            }

        return {'analysis': analysis, 'suggestions': suggestions, 'solution': solution}

    def _extract_error_type(self, error_message: str) -> Optional[str]:
        """提取错误类型"""
        patterns = [
            r'java\.lang\.(\w+Exception)',
            r'java\.lang\.(\w+Error)',
            r'(\w+Exception):',
            r'junit\.framework\.(\w+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, error_message)
            if match:
                return match.group(1)

        # 检查常见错误类型
        if 'assertion' in error_message.lower():
            return 'AssertionError'
        if 'timeout' in error_message.lower():
            return 'TimeoutException'
        if 'null' in error_message.lower():
            return 'NullPointerException'

        return None

    def _extract_keywords_from_error(self, error_message: str) -> List[str]:
        """从错误信息中提取关键字"""
        keywords = []

        patterns = [
            r"'([^']+)'",  # 单引号内容
            r'"([^"]+)"',  # 双引号内容
            r'not found:\s*(\w+)',
            r'undefined\s+(\w+)',
            r'cannot\s+(\w+)',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, error_message)
            keywords.extend(matches)

        return list(set(keywords))[:5]

    def _extract_failing_line(self, stack_trace: Optional[str]) -> Optional[int]:
        """从堆栈跟踪中提取失败行号"""
        if not stack_trace:
            return None

        # 查找行号模式
        match = re.search(r':(\d+)\)', stack_trace)
        if match:
            return int(match.group(1))

        return None

    def _extract_relevant_code(
        self,
        source_code: str,
        method_name: Optional[str],
        error_message: str
    ) -> List[Dict]:
        """提取相关代码片段"""
        relevant = []

        try:
            lines = source_code.split('\n')

            # 如果有方法名，查找该方法
            if method_name:
                method_info = self._find_method_in_code(lines, method_name)
                if method_info:
                    relevant.append(method_info)

            # 查找错误相关关键字
            error_keywords = self._extract_keywords_from_error(error_message)
            for keyword in error_keywords[:2]:
                keyword_info = self._find_keyword_in_code(lines, keyword)
                if keyword_info:
                    relevant.append(keyword_info)

        except Exception as e:
            logger.error(f"提取相关代码失败: {e}")

        return relevant

    def _find_method_in_code(self, lines: List[str], method_name: str) -> Optional[Dict]:
        """在代码中查找方法"""
        try:
            method_pattern = re.compile(
                r'(?:public|private|protected)?\s*(?:static)?\s*[\w<>\[\]]+\s+' +
                re.escape(method_name) + r'\s*\([^)]*\)\s*(?:throws\s+[\w\s,]+)?\s*\{',
                re.MULTILINE
            )

            for i, line in enumerate(lines):
                if method_pattern.search(line) or (method_name in line and '{' in line):
                    # 找到方法开始，提取方法体
                    brace_count = 0
                    start_line = i
                    end_line = i

                    for j in range(i, min(i + 200, len(lines))):  # 限制查找范围
                        brace_count += lines[j].count('{')
                        brace_count -= lines[j].count('}')
                        if brace_count == 0 and j > i:
                            end_line = j
                            break

                    # 提取方法代码（带上下文）
                    context_start = max(0, start_line - 2)
                    context_end = min(len(lines), end_line + 3)

                    return {
                        'type': 'method',
                        'name': method_name,
                        'start_line': context_start + 1,
                        'end_line': context_end + 1,
                        'code': '\n'.join(lines[context_start:context_end])
                    }

        except Exception as e:
            logger.debug(f"查找方法失败: {e}")

        return None

    def _find_keyword_in_code(self, lines: List[str], keyword: str) -> Optional[Dict]:
        """在代码中查找关键字"""
        try:
            keyword_lower = keyword.lower()
            for i, line in enumerate(lines):
                if keyword_lower in line.lower():
                    context_start = max(0, i - 2)
                    context_end = min(len(lines), i + 3)

                    return {
                        'type': 'keyword_match',
                        'keyword': keyword,
                        'line_number': i + 1,
                        'code': '\n'.join(lines[context_start:context_end])
                    }
        except Exception as e:
            logger.debug(f"查找关键字失败: {e}")

        return None


# 全局实例
source_analyzer = AndroidSourceAnalyzer()
