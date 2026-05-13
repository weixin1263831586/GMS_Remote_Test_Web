"""
通用AI模型管理器
"""

import requests
import logging
import json
from typing import Dict, Optional, List
from enum import Enum

logger = logging.getLogger(__name__)

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

            response = requests.post(url, headers=headers, json=data, timeout=self.timeout)

            if response.status_code == 200:
                result = response.json()

                # 支持两种响应格式：Anthropic 和 OpenAI
                if api_format == self.API_FORMAT_ANTHROPIC and 'content' in result:
                    # Anthropic 格式
                    content = result['content'][0].get('text', '')
                elif 'choices' in result:
                    # OpenAI 格式（智谱AI等）
                    content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
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
请分析上述信息并按以下JSON格式返回（只返回JSON，不要有其他内容）：
```json
{
  "root_cause": "🎯 根本原因：一句话总结失败的核心原因（不超过50字）",
  "analysis": "📊 详细分析：\\n1. 错误类型\\n2. 触发条件\\n3. 影响范围\\n4. 相关代码逻辑",
  "suggestions": [
    "✅ 建议一：具体的修改步骤",
    "✅ 建议二：验证方法",
    "✅ 建议三：预防措施"
  ],
  "solution": {
    "problem_description": "详细问题描述",
    "error_type": "错误类型分类",
    "fix_strategy": "修复策略说明",
    "code_example": "代码示例（Java格式）"
  }
}
```

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

4. 只返回JSON格式，不要有markdown标记或其他文字
"""
        return prompt

    def _parse_response(self, response_text: str) -> Dict:
        """解析LLM响应"""
        try:
            # 移除可能的markdown标记
            text = response_text.strip()
            if text.startswith('```json'):
                text = text[7:]
            if text.startswith('```'):
                text = text[3:]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

            # 查找JSON
            start = text.find('{')
            end = text.rfind('}') + 1

            if start >= 0 and end > start:
                json_str = text[start:end]
                try:
                    parsed = json.loads(json_str)

                    # 确保emoji前缀
                    if 'root_cause' in parsed and not parsed['root_cause'].startswith('🎯'):
                        parsed['root_cause'] = f"🎯 {parsed['root_cause']}"

                    if 'analysis' in parsed and not parsed['analysis'].startswith('📊'):
                        parsed['analysis'] = f"📊 {parsed['analysis']}"

                    if 'suggestions' in parsed:
                        parsed['suggestions'] = [
                            f"✅ {s}" if not s.startswith('✅') else s
                            for s in parsed['suggestions']
                        ]

                    return parsed
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON解析失败: {str(e)}，回退到文本解析")
                    return self._parse_text_response(response_text)
            else:
                logger.warning("未找到JSON格式，回退到文本解析")
                return self._parse_text_response(response_text)

        except json.JSONDecodeError:
            return self._parse_text_response(response_text)

    def _parse_text_response(self, text: str) -> Dict:
        """解析文本格式响应"""
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
        root_cause = f"🎯 {analysis[0]}" if analysis else "🎯 测试失败，需要进一步分析"

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

    def _fetch_source_code_android(self, class_name: str) -> Optional[Dict]:
        """
        使用OpenGrok获取Android源码

        Args:
            class_name: 测试类名（如 com.android.angleallowlists.vts.AngleAllowlistTraceTest）

        Returns:
            dict: 包含源码信息，如果失败返回None
        """
        return None


# 全局实例
_universal_analyzer = None

def get_universal_analyzer() -> UniversalAIAnalyzer:
    """获取通用AI分析器实例"""
    global _universal_analyzer
    from core.config import config_manager
    config = config_manager.load_config()
    ai_config = config.get('ai_models', {})

    # 每次都重新创建实例以确保使用最新配置
    _universal_analyzer = UniversalAIAnalyzer(ai_config)
    return _universal_analyzer
