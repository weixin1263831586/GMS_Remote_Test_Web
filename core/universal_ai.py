"""
通用AI模型管理器
"""

import requests
import logging
import json
import re
import time
from typing import Dict, Optional, List
from enum import Enum

logger = logging.getLogger(__name__)

# Emoji 常量用于 AI 分析结果
EMOJI_TARGET = "🎯"
EMOJI_CHART = "📊"
EMOJI_CHECK = "✅"

class UniversalAIAnalyzer:
    """通用AI模型分析器"""

    def __init__(self, config: Dict = None):
        """
        初始化通用AI分析器

        Args:
            config: AI模型配置字典
        """
        self.config = config or {}
        self.timeout = 60
        # API 格式常量
        self.API_FORMAT_ANTHROPIC = 'anthropic'  # /v1/messages 端点
        self.API_FORMAT_OPENAI = 'openai'  # /v1/chat/completions 端点


    def _get_api_format(self, provider_name: str, config: Dict) -> str:
        """
        获取提供商的 API 格式

        Args:
            provider_name: 提供商名称
            config: 提供商配置

        Returns:
            API 格式：'anthropic' 或 'openai'
        """
        # 优先使用配置中的 api_format 字段
        api_format = config.get('api_format')
        if api_format:
            return api_format

        # 根据 base_url 推断
        base_url = config.get('base_url', '')
        if 'anthropic' in base_url.lower() or base_url.endswith('/messages'):
            return self.API_FORMAT_ANTHROPIC

        # 默认使用 OpenAI 格式
        return self.API_FORMAT_OPENAI

    def get_primary_provider(self) -> Optional[str]:
        """获取主要提供商"""
        if not self.config.get('enabled', False):
            return None

        # 使用配置中的 primary_provider，默认为第一个启用的提供商
        primary = self.config.get('primary_provider')
        providers = self.config.get('providers', {})

        # 如果指定了主要提供商且已启用，返回它
        if primary and providers.get(primary, {}).get('enabled', False):
            return primary

        # 否则返回第一个启用的提供商
        for provider_name, provider_config in providers.items():
            if provider_config.get('enabled', False):
                return provider_name

        return None

    def analyze_test_failure(
        self,
        class_name: str,
        method_name: Optional[str],
        error_message: str,
        stack_trace: Optional[str] = None,
        source_code: Optional[str] = None,
        auto_fetch_source: bool = True
    ) -> Dict:
        """
        使用AI模型分析测试失败（自动获取源码）

        Args:
            class_name: 测试类名
            method_name: 测试方法名
            error_message: 错误信息
            stack_trace: 堆栈跟踪
            source_code: 源码（可选，如果为空且auto_fetch_source=True则自动获取）
            auto_fetch_source: 是否自动使用OpenGrok获取源码

        Returns:
            dict: 分析结果，包含源码信息
        """
        result = {
            'success': False,
            'error': None,
            'analysis': None,
            'suggestions': [],
            'solution': None,
            'provider': None,
            'source_info': None  # 新增：源码信息
        }

        try:
            # 自动获取源码（如果需要）
            if auto_fetch_source and not source_code:
                source_info = self._fetch_source_code_android(class_name)
                if source_info and source_info.get('content'):
                    source_code = source_info['content']
                    result['source_info'] = source_info
                    logger.info(f"成功获取源码: {source_info.get('file_path', 'unknown')}")

            # 获取启用的提供商
            provider_name = self.get_primary_provider()

            if not provider_name:
                result['error'] = '未找到启用的AI模型提供商'
                return result

            providers = self.config.get('providers', {})
            provider_config = providers.get(provider_name, {})

            logger.info(f"使用AI提供商: {provider_name}")

            provider_result = self._call_aimodel(provider_name, provider_config, class_name, method_name, error_message, stack_trace, source_code)

            if provider_result.get('success'):
                result['success'] = True
                result['root_cause'] = provider_result.get('root_cause', '')
                result['analysis'] = provider_result.get('analysis')
                result['suggestions'] = provider_result.get('suggestions', [])
                result['solution'] = provider_result.get('solution')
                result['provider'] = provider_name
            else:
                result['error'] = provider_result.get('error', '分析失败')

        except Exception as e:
            logger.error(f"AI分析失败: {e}")
            result['error'] = f'分析失败: {str(e)}'

        return result

    def _call_aimodel(self, provider_name: str, config: Dict, class_name: str, method_name: Optional[str],
                                error_message: str, stack_trace: Optional[str], source_code: Optional[str]) -> Dict:
        """
        Args:
            provider_name: 提供商名称
            config: 提供商配置
            class_name: 测试类名
            method_name: 测试方法名
            error_message: 错误信息
            stack_trace: 堆栈跟踪
            source_code: 源码

        Returns:
            分析结果字典
        """
        try:
            api_key = config.get('api_key', '')
            if not api_key:
                return {'success': False, 'error': f'{provider_name} API密钥未配置'}

            base_url = config.get('base_url')
            model = config.get('model')

            if not base_url:
                return {'success': False, 'error': f'{provider_name} base_url 未配置'}
            if not model:
                return {'success': False, 'error': f'{provider_name} 模型未配置'}

            prompt = self._build_prompt(class_name, method_name, error_message, stack_trace, source_code)

            # 获取 API 格式（优先使用配置中的 api_format 字段）
            api_format = self._get_api_format(provider_name, config)

            # 根据 API 格式构建请求
            if api_format == self.API_FORMAT_ANTHROPIC:
                # Anthropic 格式：/v1/messages
                url = f"{base_url}/v1/messages" if not base_url.endswith('/messages') else base_url
                headers = {
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01"
                }
                data = {
                    "model": model,
                    "max_tokens": config.get('max_tokens', 2000),
                    "messages": [{"role": "user", "content": prompt}]
                }
            else:
                # OpenAI 格式：/v1/chat/completions
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                data = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": config.get('temperature', 0.3),
                    "max_tokens": config.get('max_tokens', 2000)
                }
                url = f"{base_url}/v1/chat/completions" if not (base_url.endswith('/chat/completions') or base_url.endswith('/completions')) else base_url

            logger.info(f"[{provider_name}] Request URL: {url}, Model: {model}, Format: {api_format}")

            # 增加重试机制和更好的错误处理
            max_retries = 2
            retry_delay = 1

            for attempt in range(max_retries):
                try:
                    response = requests.post(
                        url,
                        headers={'Connection': 'keep-alive', **headers},
                        json=data,
                        timeout=self.timeout
                    )
                    break  # 成功则退出重试循环
                except requests.exceptions.ConnectionError as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"[{provider_name}] 连接失败，{retry_delay}秒后重试 ({attempt+1}/{max_retries}): {str(e)[:100]}")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # 指数退避
                    else:
                        return {'success': False, 'error': f'连接失败: {str(e)}'}
                except requests.exceptions.Timeout as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"[{provider_name}] 请求超时，{retry_delay}秒后重试 ({attempt+1}/{max_retries})")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        return {'success': False, 'error': f'请求超时: {str(e)}'}

            if response.status_code == 200:
                result = response.json()

                # 支持两种响应格式：Anthropic 和 OpenAI
                if api_format == self.API_FORMAT_ANTHROPIC and 'content' in result:
                    # Anthropic 格式
                    content = result['content'][0].get('text', '')
                elif 'choices' in result:
                    # OpenAI 格式（智谱AI等）
                    choice = result.get('choices', [{}])[0]
                    message = choice.get('message', {})
                    content = message.get('content', '')

                    # 如果 content 为空，检查是否为推理模型且被截断
                    finish_reason = choice.get('finish_reason', '')
                    if not content and message.get('reasoning'):
                        reasoning = message.get('reasoning', '')

                        # 检查是否因长度限制被截断
                        if finish_reason == 'length':
                            logger.warning(f"[{provider_name}] 响应被截断（max_tokens太小），只有 reasoning 字段")
                            # 尝试从 reasoning 中提取 JSON
                            content = self._extract_json_from_reasoning(reasoning)
                            if not content:
                                content = reasoning
                        else:
                            content = reasoning
                        logger.info(f"[{provider_name}] Content 为空，使用 reasoning 字段（长度: {len(content)}）")

                    # 如果仍然为空，尝试其他可能的字段
                    if not content:
                        content = message.get('text', '')
                        logger.info(f"[{provider_name}] Reasoning 也为空，尝试 text 字段")

                    # 最后的回退：将整个 choice 转为字符串
                    if not content:
                        content = str(choice)
                        logger.warning(f"[{provider_name}] 所有字段都为空，使用完整响应")

                    logger.debug(f"[{provider_name}] Extracted content length: {len(content)}")
                else:
                    content = str(result)

                parsed = self._parse_response(content)

                return {
                    'success': True,
                    'root_cause': parsed.get('root_cause', ''),
                    'analysis': parsed.get('analysis'),
                    'suggestions': parsed.get('suggestions', []),
                    'solution': parsed.get('solution')
                }
            else:
                try:
                    error_data = response.json()
                    error_msg = error_data.get('error', {}).get('message', f'HTTP {response.status_code}')
                except:
                    error_msg = f'HTTP {response.status_code}: {response.text[:100]}'
                return {'success': False, 'error': f'{provider_name} API错误: {error_msg}'}

        except Exception as e:
            return {'success': False, 'error': f'{provider_name}调用失败: {str(e)}'}

    def _safe_import(self, module_path: str, error_result=None):
        """
        安全导入模块，失败时返回默认值

        Args:
            module_path: 模块路径
            error_result: 导入失败时的返回值

        Returns:
            模块对象或 error_result
        """
        try:
            from importlib import import_module
            return import_module(module_path)
        except ImportError:
            logger.warning(f"{module_path} 不可用")
            return error_result

    def _build_prompt(self, class_name: str, method_name: Optional[str],
                      error_message: str, stack_trace: Optional[str], source_code: Optional[str]) -> str:
        """构造分析提示词"""
        prompt = f"""你是一个专业的Android测试代码分析专家。请分析以下测试失败信息：

**测试类名**: {class_name}
"""

        if method_name:
            prompt += f"**测试方法**: {method_name}\n"

        prompt += f"""
**错误信息**:
```
{error_message}
```
"""

        if stack_trace:
            prompt += f"""
**堆栈跟踪**:
```
{stack_trace[:2000]}
```
"""

        if source_code:
            prompt += f"""
**相关源码**:
```java
{source_code[:3000]}
```
"""

        prompt += """
请分析上述信息并按以下JSON格式返回。

**重要要求**：
1. 只返回纯JSON格式，不要包含markdown代码块标记（```json 或 ```）
2. 不要包含任何解释性文字，直接以 { 开始，以 } 结束
3. 确保JSON格式完全正确，可以被标准JSON解析器解析

返回格式：
{
  "root_cause": EMOJI_TARGET + " 根本原因描述（不超过50字）",
  "analysis": EMOJI_CHART + " 详细分析：\\n1. 错误类型：xxx\\n2. 触发条件：xxx\\n3. 影响范围：xxx\\n4. 相关代码逻辑：xxx",
  "suggestions": [
    EMOJI_CHECK + "建议一：具体的修改步骤",
    EMOJI_CHECK + "建议二：验证方法",
    EMOJI_CHECK + "建议三：预防措施"
  ],
  "solution": {
    "problem_description": "详细问题描述",
    "error_type": "错误类型分类",
    "fix_strategy": "修复策略说明",
    "code_example": "代码示例（Java格式）"
  }
}

分析要求（适用于所有类型报错）：
1. **root_cause必须以🎯开头**，一句话精准定位核心问题：
   - 配置问题："配置项xxx缺失/错误/不匹配"
   - 权限问题："缺少xxx权限导致操作失败"
   - 依赖问题："xxx依赖缺失/版本不兼容"
   - 超时问题："xxx操作超时（超过N秒）"
   - 断言失败："期望值xxx与实际值yyy不匹配"
   - 空指针/异常："调用xxx方法时抛出异常"

2. **analysis必须以📊开头**，4个维度详细分析：
   - 错误类型：明确异常类型（AssertionError/NullPointerException/TimeoutException等）
   - 触发条件：什么场景/输入/状态下触发
   - 影响范围：影响哪些模块/测试/功能
   - 相关代码逻辑：涉及的关键代码/配置/API调用

3. **suggestions必须以✅开头**，每条建议：
   - 具体可操作（避免"检查相关配置"这种模糊说法）
   - 针对根本原因（不是绕过问题）
   - 优先级排序（先解决根本问题，再考虑workaround）

4. 只返回纯JSON格式，不要有markdown标记或其他文字
"""
        return prompt

    def _extract_json_from_reasoning(self, reasoning: str) -> Optional[str]:
        """从 reasoning 字段中提取最终的 JSON 输出

        推理模型可能在 reasoning 的最后部分输出最终的 JSON，
        我们需要查找最后一个完整的 JSON 对象。
        """
        try:
            logger.debug(f"[AI Parse] Reasoning 长度: {len(reasoning)}, 最后 500 字符: {reasoning[-500:]}")

            # 查找所有可能的 JSON 对象（从 { 开始到对应的 } 结束）
            json_candidates = []
            depth = 0
            start = -1
            in_string = False
            escape_next = False

            for i, char in enumerate(reasoning):
                if escape_next:
                    escape_next = False
                    continue

                if char == '\\':
                    escape_next = True
                    continue
                elif char == '"' and not escape_next:
                    in_string = not in_string
                    continue

                # 只在非字符串状态下计算括号
                if not in_string:
                    if char == '{':
                        if depth == 0:
                            start = i
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0 and start >= 0:
                            json_candidates.append((start, i, reasoning[start:i+1]))
                            start = -1

            logger.info(f"[AI Parse] 找到 {len(json_candidates)} 个 JSON 候选")

            # 尝试解析最后一个（且最长的）JSON 对象（通常是最终输出）
            # 按长度排序，优先尝试较长的 JSON（更完整）
            json_candidates.sort(key=lambda x: len(x[2]), reverse=True)

            for start_pos, end_pos, json_str in json_candidates:
                try:
                    parsed = json.loads(json_str)
                    # 验证是否包含预期的字段且格式正确
                    if all(key in parsed for key in ['root_cause', 'analysis']):
                        # 进一步验证：检查值不是以 "Thinking" 或 "**" 开头（排除思考过程）
                        root_cause = parsed.get('root_cause', '')
                        analysis = parsed.get('analysis', '')

                        if not (root_cause.startswith('Thinking') or
                                root_cause.startswith('**') or
                                analysis.startswith('Thinking') or
                                analysis.startswith('**')):
                            logger.info(f"[AI Parse] 从 reasoning 中提取到有效 JSON (位置 {start_pos}-{end_pos}, 长度: {len(json_str)})")
                            logger.debug(f"[AI Parse] 提取的 JSON keys: {list(parsed.keys())}")
                            return json_str
                        else:
                            logger.debug(f"[AI Parse] 跳过思考过程 JSON (位置 {start_pos}-{end_pos})")
                except json.JSONDecodeError as e:
                    logger.debug(f"[AI Parse] JSON 解析失败: {e}, 字符串: {json_str[:100]}...")
                    continue

            logger.warning("[AI Parse] 未能在 reasoning 中找到有效的 JSON")
            return None

        except Exception as e:
            logger.error(f"[AI Parse] 提取 JSON 时出错: {e}")
            return None

    def _parse_response(self, response_text: str) -> Dict:
        """解析LLM响应"""
        try:
            # 检查输入是否为空或None
            if not response_text:
                logger.warning("AI响应为空，返回默认分析结果")
                return {
                    'root_cause': '🎯 AI响应解析失败',
                    'analysis': '📊 AI模型返回了空响应，可能是API配置问题或模型错误',
                    'suggestions': [
                        '✅ 检查AI模型配置是否正确',
                        '✅ 查看后端日志了解详细错误信息',
                        '✅ 尝试使用基于规则的分析'
                    ]
                }

            logger.debug(f"[AI Parse] 响应长度: {len(response_text)}, 前500字符: {response_text[:500]}")

            # 移除可能的markdown标记
            text = response_text.strip()

            # 优化：一次性移除所有markdown代码块标记，包括各种变体
            text = re.sub(r'```(?:json|JSON|javascript)?', '', text, flags=re.IGNORECASE)
            text = text.strip()

            # 查找JSON对象
            start = text.find('{')
            end = text.rfind('}') + 1

            if start >= 0 and end > start:
                json_str = text[start:end]

                # 尝试多层次的JSON修复策略
                parsed = None
                parse_attempts = [
                    # 1. 直接解析
                    lambda: json.loads(json_str),
                    # 2. 清理控制字符后解析
                    lambda: json.loads(''.join(char for char in json_str if char.isprintable() or char in '\n\r\t')),
                    # 3. 规范化空白后解析
                    lambda: json.loads(' '.join(''.join(char for char in json_str if char.isprintable() or char in '\n\r\t').split())),
                    # 4. 移除注释后解析
                    lambda: json.loads(re.sub(r'//.*?\n|/\*.*?\*/', '', json_str)),
                    # 5. 修复常见的中英文标点问题
                    lambda: json.loads(json_str.replace('，', ',').replace('：', ':').replace('"', '"').replace('"', '"'))
                ]

                for i, attempt in enumerate(parse_attempts):
                    try:
                        parsed = attempt()
                        if i > 0:
                            logger.info(f"JSON解析成功（使用了策略{i+1}）")
                        break
                    except (json.JSONDecodeError, ValueError) as e:
                        if i < len(parse_attempts) - 1:
                            logger.debug(f"JSON解析策略{i+1}失败: {str(e)[:100]}")
                        continue

                if parsed is None:
                    logger.warning(f"所有JSON解析策略都失败，回退到文本解析。原始JSON片段: {json_str[:200]}")
                    return self._parse_text_response(response_text)

                if 'root_cause' in parsed and not parsed['root_cause'].startswith('🎯'):
                    parsed['root_cause'] = f"{EMOJI_TARGET} {parsed['root_cause']}"

                if 'analysis' in parsed and not parsed['analysis'].startswith('📊'):
                    parsed['analysis'] = f"{EMOJI_CHART}  {parsed['analysis']}"

                if 'suggestions' in parsed:
                    parsed['suggestions'] = [
                        f"{EMOJI_CHECK}  {s}" if not s.startswith('✅') else s
                        for s in parsed['suggestions']
                    ]

                # 验证必需字段
                if not all(key in parsed for key in ['root_cause', 'analysis', 'suggestions']):
                    logger.warning(f"[AI Parse] JSON缺少必需字段，现有字段: {list(parsed.keys())}")
                    # 补充缺失字段
                    if 'root_cause' not in parsed:
                        parsed['root_cause'] = '🎯 AI分析结果不完整'
                    if 'analysis' not in parsed:
                        parsed['analysis'] = '📊 AI未返回详细分析'
                    if 'suggestions' not in parsed:
                        parsed['suggestions'] = ['✅ 查看源码和错误信息进行排查']

                logger.info(f"[AI Parse] JSON解析成功，包含字段: {list(parsed.keys())}")
                return parsed
            else:
                logger.warning("未找到JSON格式，回退到文本解析")
                return self._parse_text_response(response_text)

        except Exception as e:
            logger.error(f"解析响应时发生异常: {str(e)}，回退到文本解析")
            return self._parse_text_response(response_text)

    def _parse_text_response(self, text: str) -> Dict:
        """解析文本格式响应（回退方案）"""
        try:
            logger.info("[AI Parse] 使用文本解析回退方案")

            lines = text.split('\n')
            analysis = []
            suggestions = []

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if '分析' in line or '问题' in line:
                    continue
                elif line.startswith('-') or line.startswith('*') or line.startswith('•'):
                    suggestions.append(line.lstrip('-*•').strip())
                elif line.startswith('建议') or 'suggest' in line.lower():
                    suggestions.append(line)
                elif line:
                    if not suggestions:
                        analysis.append(line)
                    else:
                        # 如果已经开始收集建议，继续添加
                        if len(suggestions) > 0 or '建议' in line:
                            suggestions.append(line)

            # 从分析文本中提取根本原因（第一行）
            root_cause = f"{EMOJI_TARGET} {analysis[0]}" if analysis else EMOJI_TARGET + " 测试失败，需要进一步分析"

            # 如果没有提取到任何有用信息，返回基于规则的分析
            if not analysis and not suggestions:
                logger.warning("[AI Parse] 文本解析也未提取到有用信息，返回通用分析")
                return self._get_fallback_analysis()

            return {
                'root_cause': root_cause,
                'analysis': '\n'.join(analysis) if analysis else '无法解析详细分析',
                'suggestions': suggestions if suggestions else ['查看源码和错误信息进行排查'],
                'solution': {
                    'problem_description': '\n'.join(analysis[:3]) if analysis else '需要进一步分析',
                    'error_type': 'Unknown',
                    'fix_strategy': 'manual_review'
                }
            }
        except Exception as e:
            logger.error(f"[AI Parse] 文本解析也失败: {str(e)}，返回默认分析")
            return self._get_fallback_analysis()

    def _get_fallback_analysis(self) -> Dict:
        """获取回退分析结果"""
        return {
            'root_cause': '🎯 AI响应格式异常，无法解析',
            'analysis': '📊 AI模型返回了无法识别的格式。可能原因：\n1. AI模型配置错误\n2. API返回格式不符合预期\n3. 网络传输问题导致数据损坏',
            'suggestions': [
                '✅ 检查AI模型配置是否正确',
                '✅ 查看后端日志了解详细错误信息',
                '✅ 尝试重新进行分析',
                '✅ 联系管理员检查AI服务状态'
            ],
            'solution': {
                'problem_description': 'AI模型返回格式异常，需要技术支持',
                'error_type': 'AI_Parse_Error',
                'fix_strategy': '检查AI配置和日志',
                'code_example': '// 无法提供代码示例，因为AI响应格式异常'
            }
        }

    def _fetch_source_code_android(self, class_name: str) -> Optional[Dict]:
        """
        使用OpenGrok获取Android源码

        Args:
            class_name: 测试类名（如 com.android.angleallowlists.vts.AngleAllowlistTraceTest）

        Returns:
            dict: 包含源码信息，如果失败返回None
        """
        try:
            # 参数验证
            if not class_name or not isinstance(class_name, str):
                logger.warning(f"无效的类名参数: {class_name}")
                return None

            # 使用本地的 rk_codesearch 技能来查找源码
            from core.report_analyzer import ReportAnalyzer

            # 创建临时分析器实例
            temp_analyzer = ReportAnalyzer()

            # 调用 rk_codesearch 方法搜索源码
            search_results = temp_analyzer.rk_codesearch(class_name, max_results=3)

            if search_results and len(search_results) > 0:
                # 取第一个搜索结果
                first_result = search_results[0]

                # 构建返回结果
                result = {
                    'file_path': first_result.get('path', ''),
                    'line': first_result.get('line', ''),
                    'file_type': first_result.get('file_type', 'java'),
                    'project': first_result.get('project', ''),
                    'url': first_result.get('url', ''),
                    'content': f"// 源码文件: {first_result.get('path', '')}\n" +
                              f"// 项目: {first_result.get('project', '')}\n" +
                              f"// OpenGrok链接: {first_result.get('url', '未找到链接')}\n" +
                              f"//\n" +
                              f"// 注意: 完整源码内容请通过OpenGrok链接查看\n" +
                              f"// 该文件为 {first_result.get('file_type', 'java')} 格式\n"
                }

                logger.info(f"成功通过 rk_codesearch 获取源码信息: {first_result.get('path', 'unknown')}")
                return result
            else:
                logger.warning(f"rk_codesearch 未找到类 {class_name} 的源码")
                return None

        except ImportError:
            logger.error("无法导入 ReportAnalyzer，请检查 core.report_analyzer 模块")
            return None
        except Exception as e:
            logger.error(f"获取Android源码失败: {e}")
            return None


# 全局实例
_universal_analyzer = None

def get_universal_analyzer() -> UniversalAIAnalyzer:
    """获取通用AI分析器实例"""
    global _universal_analyzer
    from core.config import config_manager

    ai_config = config_manager.get_ai_config()

    # 每次都重新创建实例以确保使用最新配置
    _universal_analyzer = UniversalAIAnalyzer(ai_config)
    return _universal_analyzer
