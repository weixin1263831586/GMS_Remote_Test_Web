"""
Android源码分析器
获取Android源码并分析失败原因，给出修改建议
"""

import re
import requests
import logging
import base64
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

logger = logging.getLogger(__name__)


class AndroidSourceAnalyzer:
    """Android源码分析器"""

    def __init__(self):
        self.base_url = "https://cs.android.com/android/platform/superproject"
        self.timeout = 15

    def fetch_source_code(self, class_name: str, package: Optional[str] = None) -> Optional[Dict]:
        """
        获取Android源码

        Args:
            class_name: 类名（如 AngleAllowlistTraceTest）
            package: 包名（如 com.google.android.angleallowlists.vts）

        Returns:
            dict: 包含源码信息，如果失败返回None
        """
        try:
            # 构造文件名和搜索路径
            simple_class_name = class_name.split('.')[-1]
            filename = f"{simple_class_name}.java"

            # 构造可能的文件路径
            possible_paths = self._guess_file_paths(simple_class_name, package)

            logger.info(f"尝试获取源码: {simple_class_name}")

            # 尝试多个策略获取源码
            for path in possible_paths:
                # 策略1: 使用Android Code Search API
                content = self._fetch_via_code_search_api(path)
                if content:
                    logger.info(f"通过API成功获取源码: {path}")
                    return {
                        'class_name': simple_class_name,
                        'file_path': path,
                        'source_url': f"{self.base_url}/android/platform/superproject/+/android-latest-release:{path}",
                        'content': content,
                        'filename': filename,
                        'fetch_method': 'api'
                    }

                # 策略2: 尝试使用GitHub镜像
                content = self._fetch_via_github_mirror(path)
                if content:
                    logger.info(f"通过GitHub镜像获取源码: {path}")
                    return {
                        'class_name': simple_class_name,
                        'file_path': path,
                        'source_url': f"https://github.com/android/platform-superproject/blob/android-latest-release/{path}",
                        'content': content,
                        'filename': filename,
                        'fetch_method': 'github'
                    }

            logger.warning(f"无法获取源码: {class_name}")
            return None

        except Exception as e:
            logger.error(f"获取源码失败: {e}")
            return None

    def _fetch_via_code_search_api(self, file_path: str) -> Optional[str]:
        """通过Android Code Search API获取文件内容"""
        try:
            # 方法1: 尝试直接文本格式
            url = f"{self.base_url}/android/platform/superproject/+/android-latest-release:{file_path}?format=TEXT"
            response = requests.get(url, timeout=self.timeout, headers={'Accept': 'text/plain'})

            if response.status_code == 200 and response.text:
                content = response.text
                # 检查是否是base64编码
                if self._is_base64(content):
                    try:
                        return base64.b64decode(content).decode('utf-8')
                    except:
                        pass
                # 检查是否是有效的Java代码
                if 'package ' in content or 'import ' in content or 'class ' in content:
                    return content

            # 方法2: 尝试JSON API
            api_url = f"{self.base_url}/_go/ ../android/platform/superproject/+/android-latest-release:{file_path}?format=JSON"
            response = requests.get(api_url, timeout=self.timeout)

            if response.status_code == 200:
                try:
                    data = response.json()
                    if 'content' in data:
                        content = data['content']
                        if isinstance(content, str):
                            # 可能是base64编码
                            try:
                                return base64.b64decode(content).decode('utf-8')
                            except:
                                return content
                except:
                    pass

            return None

        except Exception as e:
            logger.debug(f"API获取失败: {e}")
            return None

    def _fetch_via_github_mirror(self, file_path: str) -> Optional[str]:
        """通过GitHub镜像获取文件内容"""
        try:
            # GitHub上的Android平台镜像
            github_urls = [
                f"https://raw.githubusercontent.com/android/platform-superproject/main/{file_path}",
                f"https://raw.githubusercontent.com/aosp-mirror/platform_superproject/master/{file_path}",
            ]

            for url in github_urls:
                response = requests.get(url, timeout=self.timeout)
                if response.status_code == 200:
                    content = response.text
                    if 'package ' in content or 'import ' in content or 'class ' in content:
                        return content

            return None

        except Exception as e:
            logger.debug(f"GitHub获取失败: {e}")
            return None

    def _is_base64(self, s: str) -> bool:
        """检查字符串是否是base64编码"""
        try:
            if len(s) % 4 != 0:
                return False
            if not re.match(r'^[A-Za-z0-9+/]+={0,2}$', s):
                return False
            # 尝试解码
            decoded = base64.b64decode(s)
            # 检查解码后是否包含可打印字符
            return any(32 <= byte < 127 for byte in decoded)
        except:
            return False

    def _guess_file_paths(self, class_name: str, package: Optional[str] = None) -> List[str]:
        """
        根据类名和包名猜测可能的文件路径

        Args:
            class_name: 简单类名（如 AngleAllowlistTraceTest）
            package: 完整包名（如 com.google.android.angleallowlists.vts）

        Returns:
            list: 可能的文件路径列表
        """
        paths = []

        # 如果有包名，构造标准路径
        if package:
            # 将包名转换为路径
            package_path = package.replace('.', '/')
            # 测试文件通常在 test/ 目录下
            paths.append(f"test/{package_path}/{class_name}.java")

            # VTS测试的特殊路径
            if 'vts' in package_path.lower():
                # 尝试从类名推导模块名
                module_name = class_name.replace('Test', '').replace('test', '').lower()
                paths.append(f"test/vts-tests/{module_name}/host/src/{package_path}/{class_name}.java")
                paths.append(f"test/vts/tests/{module_name}/host/src/{package_path}/{class_name}.java")

        # 通用测试路径
        paths.append(f"test/{class_name}.java")

        # CTS/GTS/VTS 特定路径
        if class_name.endswith('Test'):
            module_name = class_name[:-4].lower()
            paths.append(f"test/{module_name}/{class_name}.java")

        # 添加src路径（非测试代码）
        if package:
            package_path = package.replace('.', '/')
            paths.append(f"src/{package_path}/{class_name}.java")

        logger.info(f"猜测的文件路径: {paths[:3]}")
        return paths

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
            # 获取源码
            source_info = self.fetch_source_code(class_name)

            if not source_info or not source_info.get('content'):
                logger.info("无法获取源码，使用错误模式分析")
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
                result['analysis'].append("获取的源码内容不完整")
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
                "使用源码搜索链接定位相关代码",
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
