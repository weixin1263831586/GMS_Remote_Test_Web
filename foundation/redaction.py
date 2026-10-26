"""Shared redaction for credentials embedded in free-form text.

设计要点（外部深度评审后）：

- 自由文本键值对分「带引号」和「不带引号」两段匹配：带引号的值取完整
  引号内容（可含空格与转义引号 ``\"``），不再在第一个空格或转义引号处
  截断；
- 能解析为 JSON 的字符串先走结构化脱敏；日志文本中嵌入的 JSON 片段
  （「错误前缀 + JSON 片段」）也先提取、解析，再做结构化脱敏；解析失败
  才退回保守的自由文本正则；
- 自由文本键名与结构化路径共用同一套敏感字段名（子串匹配，
  ``access_token`` 等复合键同样命中）与 token 计数豁免，两套规则不再
  各自维护；
- 仅统计语义的 token 计数字段（``input_tokens``、``token_count`` 等）
  不是凭据，保持原始类型与数值。
"""

from __future__ import annotations

import json
import re
from typing import Any


# 结构化与自由文本路径共用的敏感字段名（对键名做子串匹配）。
_SENSITIVE_FIELD_PATTERN = re.compile(
    r'password|passwd|pswd|token|secret|api[_-]?key|authorization|cookie|pair[_-]?code|private[_-]?key',
    re.IGNORECASE,
)
# token 计数/用量统计（input_tokens、token_count、total_tokens、used_token...）
# 是平台高频统计字段，不是访问凭据。
_TOKEN_COUNTER_FIELD_PATTERN = re.compile(
    r'(?:^|_)'
    r'(?:tokens?_count|count_tokens?|tokens?_used|used_tokens?'
    r'|(?:input|output|prompt|completion|max|total|cached)_tokens?'
    r'|tokens?_(?:input|output|prompt|completion|max|total|used|cached))'
    r'(?:_|$)',
    re.IGNORECASE,
)
# Identify each complete key once, then test its sensitivity. Starting inside
# a word would retry every suffix of long identifiers and make masking O(n²).
_ASSIGNMENT_KEY_PATTERN = re.compile(r'''(?<![\w-])(?P<key>[\w-]+)["']?\s*[:=]\s*''')
_QUOTED_VALUE_PATTERN = re.compile(
    r'''(?P<quote>["'])(?:\\[\s\S]|(?!(?P=quote))[^\\])*(?P<close>(?P=quote))?'''
)
_UNQUOTED_VALUE_PATTERN = re.compile(r'''[^\s,;}&"']+''')
_AUTHORIZATION_VALUE_PATTERN = re.compile(
    r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]+'
)
_OPENAI_STYLE_SECRET_PATTERN = re.compile(r'\bsk-[A-Za-z0-9_-]+\b')
_URL_CREDENTIAL_PATTERN = re.compile(r'(?i)(https?://)[^/@\s]+@')
_PLAIN_LOG_TAG_PATTERN = re.compile(r'\[[\w -]+\]')
_MAX_REDACTION_DEPTH = 32
_MAX_JSON_FRAGMENT_CHARS = 64 * 1024
_MAX_JSON_TOTAL_CHARS = 256 * 1024
_MAX_JSON_FRAGMENTS = 32


def _field_is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_FIELD_PATTERN.search(name)) and not _is_token_counter_field(name)


def _is_token_counter_field(name: str) -> bool:
    return bool(_TOKEN_COUNTER_FIELD_PATTERN.search(name))


def redact_sensitive_data(value: Any, *, known_values=(), mask: str = '***') -> Any:
    """Copy display data, masking secret fields and their occurrences in text."""
    secrets = {str(item) for item in known_values if item}
    json_budget = {'attempts': _MAX_JSON_FRAGMENTS, 'chars': _MAX_JSON_TOTAL_CHARS}

    def collect(item, secret=False, depth=0):
        if depth >= _MAX_REDACTION_DEPTH:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                collect(child, secret or _field_is_sensitive(str(key)), depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child, secret, depth + 1)
        elif secret and isinstance(item, (str, int)) and item:
            secrets.add(str(item))

    def public(item, depth=0):
        if depth >= _MAX_REDACTION_DEPTH:
            return mask
        if isinstance(item, dict):
            return {key: (mask if child else '') if _field_is_sensitive(str(key))
                    else public(child, depth + 1) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [public(child, depth + 1) for child in item]
        if isinstance(item, str):
            for secret in ordered_secrets:
                item = item.replace(secret, mask)
            item = _redact_embedded_json_fragments(
                item, lambda parsed: public(parsed, depth + 1), json_budget, mask,
            )
            return redact_sensitive_text(item)
        return item

    collect(value)
    ordered_secrets = sorted(secrets, key=len, reverse=True)
    return public(value)


_JSON_DECODER = json.JSONDecoder()


def _bounded_json_end(value: str, start: int, max_chars: int) -> int | None:
    """Find a complete container without giving unbounded input to the decoder."""
    closing: list[str] = []
    quoted = escaped = False
    for index in range(start, min(len(value), start + max_chars)):
        char = value[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '{[':
            closing.append('}' if char == '{' else ']')
            if len(closing) > _MAX_REDACTION_DEPTH:
                return None
        elif char in '}]':
            if not closing or closing.pop() != char:
                return None
            if not closing:
                return index + 1
    return None


def _redact_embedded_json_fragments(value: str, public, budget: dict[str, int], mask: str) -> str:
    """Extract and structurally redact JSON fragments embedded in log text.

    「错误前缀 + JSON 片段」类文本整体不进入 JSON 解析分支，此前只能
    退回自由文本正则；这里在每个 ``{``/``[`` 处先界定完整 JSON 容器，
    再尝试解析。扫描深度、单片段长度及一次调用的总工作量均有限制；
    超限或不完整的剩余内容整体遮蔽，保留外层审计事件及其上下文。
    """
    pieces: list[str] = []
    cursor = 0
    index = 0
    length = len(value)
    while index < length:
        if value[index] not in '{[':
            index += 1
            continue
        pieces.append(value[cursor:index])
        max_chars = min(_MAX_JSON_FRAGMENT_CHARS, budget['chars'])
        end = _bounded_json_end(value, index, max_chars) if budget['attempts'] > 0 else None
        if end is None:
            pieces.append(mask)
            return ''.join(pieces)
        fragment = value[index:end]
        budget['attempts'] -= 1
        budget['chars'] -= len(fragment)
        try:
            parsed = _JSON_DECODER.decode(fragment)
            pieces.append(json.dumps(public(parsed), ensure_ascii=False))
        except (ValueError, RecursionError):
            # Keep ordinary bracketed tags (e.g. [WARN]); invalid objects
            # cannot safely expose values hidden behind escaped JSON keys.
            pieces.append(redact_sensitive_text(fragment) if _PLAIN_LOG_TAG_PATTERN.fullmatch(fragment) else mask)
        cursor = index = end
    pieces.append(value[cursor:])
    return ''.join(pieces)


def _redact_assignments(value: str) -> str:
    pieces: list[str] = []
    cursor = position = 0
    while match := _ASSIGNMENT_KEY_PATTERN.search(value, position):
        position = match.end()
        if not _field_is_sensitive(match.group('key')):
            # Keep scanning inside ordinary values: a message may itself
            # contain a password assignment that must still be masked.
            continue
        quoted = _QUOTED_VALUE_PATTERN.match(value, position)
        secret = quoted or _UNQUOTED_VALUE_PATTERN.match(value, position)
        if secret is None:
            continue
        pieces.append(value[cursor:position])
        if quoted:
            pieces.append(quoted.group('quote') + '***REDACTED***' + (quoted.group('close') or ''))
        else:
            pieces.append('***REDACTED***')
        cursor = position = secret.end()
    pieces.append(value[cursor:])
    return ''.join(pieces)


def redact_sensitive_text(value: object) -> str:
    """Hide common credentials without changing the surrounding error text."""

    redacted = _URL_CREDENTIAL_PATTERN.sub(
        r'\1***REDACTED***@',
        str(value or ''),
    )
    redacted = _AUTHORIZATION_VALUE_PATTERN.sub(
        lambda match: f'{match.group(1)} ***REDACTED***',
        redacted,
    )
    redacted = _OPENAI_STYLE_SECRET_PATTERN.sub('***REDACTED***', redacted)
    return _redact_assignments(redacted)
