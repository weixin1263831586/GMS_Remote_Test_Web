"""
通用AI模型管理器
支持多个AI提供商：智谱AI、OpenAI、Claude、Ollama等
"""

import requests
import logging
import json
from typing import Dict, Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


class AIProvider(Enum):
    """AI提供商枚举"""
    ZHIPU = "zhipu"
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


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

    def get_primary_provider(self) -> Optional[str]:
        """获取主要提供商"""
        if not self.config.get('enabled', False):
            return None

        primary = self.config.get('primary_provider', 'zhipu')
        providers = self.config.get('providers', {})

        # 如果主要提供商未启用，返回第一个启用的
        if providers.get(primary, {}).get('enabled', False):
            return primary

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
            auto_fetch_source: 是否自动从 https://cs.android.com/android/platform/superproject 获取源码

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

            # 根据提供商调用不同的方法
            if provider_name == AIProvider.ZHIPU.value:
                provider_result = self._call_zhipu(provider_config, class_name, method_name, error_message, stack_trace, source_code)
            elif provider_name == AIProvider.OLLAMA.value:
                provider_result = self._call_ollama(provider_config, class_name, method_name, error_message, stack_trace, source_code)
            elif provider_name == AIProvider.OPENAI.value:
                provider_result = self._call_openai(provider_config, class_name, method_name, error_message, stack_trace, source_code)
            elif provider_name == AIProvider.ANTHROPIC.value:
                provider_result = self._call_anthropic(provider_config, class_name, method_name, error_message, stack_trace, source_code)
            else:
                provider_result = {'success': False, 'error': f'不支持的提供商: {provider_name}'}

            if provider_result.get('success'):
                result['success'] = True
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

    def _call_zhipu(self, config: Dict, class_name: str, method_name: Optional[str],
                    error_message: str, stack_trace: Optional[str], source_code: Optional[str]) -> Dict:
        """调用智谱AI"""
        try:
            api_key = config.get('api_key', '')
            if not api_key:
                return {'success': False, 'error': '智谱AI API密钥未配置'}

            base_url = config.get('base_url', 'https://open.bigmodel.cn/api/paas/v4/chat/completions')
            model = config.get('model', 'glm-4')

            # 构建提示词
            prompt = self._build_prompt(class_name, method_name, error_message, stack_trace, source_code)

            # 调用API
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            data = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": config.get('temperature', 0.3),
                "max_tokens": config.get('max_tokens', 2000)
            }

            response = requests.post(base_url, headers=headers, json=data, timeout=self.timeout)

            if response.status_code == 200:
                result = response.json()
                content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
                parsed = self._parse_response(content)

                return {
                    'success': True,
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
                return {'success': False, 'error': f'智谱AI API错误: {error_msg}'}

        except Exception as e:
            return {'success': False, 'error': f'智谱AI调用失败: {str(e)}'}

    def _call_ollama(self, config: Dict, class_name: str, method_name: Optional[str],
                     error_message: str, stack_trace: Optional[str], source_code: Optional[str]) -> Dict:
        """调用Ollama本地模型"""
        try:
            from core.llm_analyzer import llm_analyzer

            # 更新配置
            llm_analyzer.host = config.get('host', 'http://localhost:11434')
            llm_analyzer.model_name = config.get('model', 'deepseek-coder:6.7b')

            return llm_analyzer.analyze_test_failure(class_name, method_name, error_message, stack_trace, source_code)
        except Exception as e:
            return {'success': False, 'error': f'Ollama调用失败: {str(e)}'}

    def _call_openai(self, config: Dict, class_name: str, method_name: Optional[str],
                     error_message: str, stack_trace: Optional[str], source_code: Optional[str]) -> Dict:
        """调用OpenAI"""
        try:
            api_key = config.get('api_key', '')
            if not api_key:
                return {'success': False, 'error': 'OpenAI API密钥未配置'}

            base_url = config.get('base_url', 'https://api.openai.com/v1/chat/completions')
            model = config.get('model', 'gpt-4')

            prompt = self._build_prompt(class_name, method_name, error_message, stack_trace, source_code)

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

            response = requests.post(base_url, headers=headers, json=data, timeout=self.timeout)

            if response.status_code == 200:
                result = response.json()
                content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
                parsed = self._parse_response(content)

                return {
                    'success': True,
                    'analysis': parsed.get('analysis'),
                    'suggestions': parsed.get('suggestions', []),
                    'solution': parsed.get('solution')
                }
            else:
                return {'success': False, 'error': f'OpenAI API错误: {response.status_code}'}

        except Exception as e:
            return {'success': False, 'error': f'OpenAI调用失败: {str(e)}'}

    def _call_anthropic(self, config: Dict, class_name: str, method_name: Optional[str],
                        error_message: str, stack_trace: Optional[str], source_code: Optional[str]) -> Dict:
        """调用Claude (Anthropic)"""
        try:
            api_key = config.get('api_key', '')
            if not api_key:
                return {'success': False, 'error': 'Claude API密钥未配置'}

            base_url = config.get('base_url', 'https://api.anthropic.com/v1/messages')
            model = config.get('model', 'claude-3-sonnet-20240229')

            prompt = self._build_prompt(class_name, method_name, error_message, stack_trace, source_code)

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

            response = requests.post(base_url, headers=headers, json=data, timeout=self.timeout)

            if response.status_code == 200:
                result = response.json()
                content = result.get('content', [{}])[0].get('text', '')
                parsed = self._parse_response(content)

                return {
                    'success': True,
                    'analysis': parsed.get('analysis'),
                    'suggestions': parsed.get('suggestions', []),
                    'solution': parsed.get('solution')
                }
            else:
                return {'success': False, 'error': f'Claude API错误: {response.status_code}'}

        except Exception as e:
            return {'success': False, 'error': f'Claude调用失败: {str(e)}'}

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
  "analysis": "问题分析：简要描述失败的根本原因",
  "suggestions": [
    "建议1：具体的修改步骤",
    "建议2：验证方法",
    "建议3：预防措施"
  ],
  "solution": {
    "problem_description": "问题描述",
    "error_type": "错误类型",
    "fix_strategy": "修复策略",
    "code_example": "代码示例（如果有）"
  }
}
```

要求：
1. 分析要准确、具体
2. 建议要可操作、实用
3. 代码示例要简洁明了（使用Java）
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
                return json.loads(json_str)
            else:
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
            if '分析' in line or '问题' in line:
                continue
            elif line.startswith('-') or line.startswith('*') or line.startswith('•'):
                suggestions.append(line.lstrip('-*•').strip())
            elif line:
                if not suggestions:
                    analysis.append(line)
                else:
                    suggestions.append(line)

        return {
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
        从 https://cs.android.com/android/platform/superproject 获取Android源码

        Args:
            class_name: 测试类名（如 com.android.angleallowlists.vts.AngleAllowlistTraceTest）

        Returns:
            dict: 包含源码信息，如果失败返回None
        """
        try:
            from core.source_analyzer import source_analyzer
            # 提取简单类名
            simple_class_name = class_name.split('.')[-1]
            # 获取源码
            source_info = source_analyzer.fetch_source_code(simple_class_name)

            if source_info:
                logger.info(f"成功获取源码: {source_info.get('file_path', 'unknown')}")
                return source_info

            return None

        except Exception as e:
            logger.warning(f"获取源码失败: {e}")
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
