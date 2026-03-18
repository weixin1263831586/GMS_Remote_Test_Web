"""
本地大模型源码分析器
使用 Ollama + DeepSeek-Coder 进行智能代码分析
"""

import requests
import logging
import json
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class LLMAnalyzer:
    """本地大模型分析器"""

    def __init__(self, model_name: str = "deepseek-coder:6.7b", host: str = "http://localhost:11434"):
        """
        初始化LLM分析器

        Args:
            model_name: Ollama模型名称
            host: Ollama服务地址
        """
        self.model_name = model_name
        self.host = host
        self.api_url = f"{host}/api/generate"
        self.timeout = 60  # 增加超时时间，因为本地推理较慢

    def check_service(self) -> bool:
        """检查Ollama服务是否可用"""
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=5)
            if response.status_code == 200:
                logger.info("Ollama服务可用")
                return True
            return False
        except Exception as e:
            logger.warning(f"Ollama服务不可用: {e}")
            return False

    def check_model(self) -> bool:
        """检查模型是否已下载"""
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=5)
            if response.status_code == 200:
                models = response.json().get('models', [])
                model_names = [m.get('name', '') for m in models]
                # 检查是否有匹配的模型
                for name in model_names:
                    if self.model_name.split(':')[0] in name:
                        logger.info(f"找到模型: {name}")
                        return True
                logger.warning(f"模型 {self.model_name} 未找到")
                return False
        except Exception as e:
            logger.error(f"检查模型失败: {e}")
        return False

    def analyze_test_failure(
        self,
        class_name: str,
        method_name: Optional[str],
        error_message: str,
        stack_trace: Optional[str] = None,
        source_code: Optional[str] = None
    ) -> Dict:
        """
        使用LLM分析测试失败原因

        Args:
            class_name: 测试类名
            method_name: 测试方法名
            error_message: 错误信息
            stack_trace: 堆栈跟踪
            source_code: 源码（如果有）

        Returns:
            dict: 分析结果
        """
        result = {
            'success': False,
            'error': None,
            'analysis': None,
            'suggestions': [],
            'solution': None
        }

        try:
            # 检查服务
            if not self.check_service():
                result['error'] = 'Ollama服务未运行，请先启动Ollama'
                return result

            # 检查模型
            if not self.check_model():
                result['error'] = f'模型 {self.model_name} 未安装，请运行: ollama pull {self.model_name}'
                return result

            # 构造提示词
            prompt = self._build_analysis_prompt(
                class_name, method_name, error_message, stack_trace, source_code
            )

            # 调用LLM
            response = requests.post(
                self.api_url,
                json={
                    "model": self.model_name,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,  # 降低温度以获得更确定性的输出
                        "num_predict": 1000,  # 限制输出长度
                    }
                },
                timeout=self.timeout
            )

            if response.status_code == 200:
                llm_response = response.json()
                response_text = llm_response.get('response', '')

                # 解析LLM响应
                parsed = self._parse_llm_response(response_text)

                result['success'] = True
                result['analysis'] = parsed.get('analysis', '')
                result['suggestions'] = parsed.get('suggestions', [])
                result['solution'] = parsed.get('solution')

            else:
                result['error'] = f'LLM请求失败: {response.status_code}'

        except requests.Timeout:
            result['error'] = 'LLM推理超时，请稍后重试'
        except Exception as e:
            logger.error(f"LLM分析失败: {e}")
            result['error'] = f'分析失败: {str(e)}'

        return result

    def _build_analysis_prompt(
        self,
        class_name: str,
        method_name: Optional[str],
        error_message: str,
        stack_trace: Optional[str],
        source_code: Optional[str]
    ) -> str:
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
{stack_trace[:2000]}  # 限制长度
```
"""

        if source_code:
            prompt += f"""
**相关源码**:
```java
{source_code[:3000]}  # 限制长度
```
"""

        prompt += """
请按以下JSON格式返回分析结果（不要有其他文字，只返回JSON）：
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

注意：
1. 分析要准确、具体
2. 建议要可操作、实用
3. 代码示例要简洁明了
4. 只返回JSON，不要有其他内容
"""
        return prompt

    def _parse_llm_response(self, response_text: str) -> Dict:
        """解析LLM响应"""
        try:
            # 尝试提取JSON
            # 查找第一个 { 和最后一个 }
            start = response_text.find('{')
            end = response_text.rfind('}') + 1

            if start >= 0 and end > start:
                json_str = response_text[start:end]
                parsed = json.loads(json_str)
                return parsed
            else:
                # 如果没有找到JSON，解析文本
                return self._parse_text_response(response_text)

        except json.JSONDecodeError as e:
            logger.warning(f"JSON解析失败: {e}")
            return self._parse_text_response(response_text)

    def _parse_text_response(self, text: str) -> Dict:
        """解析文本格式响应"""
        lines = text.split('\n')
        analysis = []
        suggestions = []
        current_section = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if '分析' in line or 'Analysis' in line.lower():
                current_section = 'analysis'
            elif '建议' in line or 'Suggestion' in line.lower():
                current_section = 'suggestions'
            elif line.startswith('-') or line.startswith('*') or line.startswith('•'):
                content = line.lstrip('-*•').strip()
                if current_section == 'suggestions':
                    suggestions.append(content)
                else:
                    analysis.append(content)
            else:
                if current_section == 'analysis':
                    analysis.append(line)
                elif current_section == 'suggestions' and line:
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


# 全局实例
llm_analyzer = LLMAnalyzer()
