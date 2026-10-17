"""Shared redaction for credentials embedded in free-form text.

设计要点（外部深度评审后）：

- 自由文本键值对分「带引号」和「不带引号」两段匹配：带引号的值取完整
  引号内容（可含空格），不再在第一个空格处截断；
- 能解析为 JSON 的字符串先走结构化脱敏，等价于按对象逐字段处理；
- 仅统计语义的 token 计数字段（``input_tokens``、``token_count`` 等）
  不是凭据，保持原始类型与数值。
"""

from __future__ import annotations

import json
import re
from typing import Any


# 自由文本脱敏只认这些键名；token 计数字段单独排除（见下方）。
_SENSITIVE_NAME_PATTERN = (
    r'(?:password|passwd|pswd|token|secret|api[_-]?key|apikey|authorization|cookie|pair_code)'
)
# key["\'']?\s*[:=]\s*"..."：值取到匹配的收尾引号为止，允许内部空格。
# 转义引号（\"）不在此正则处理范围，由 JSON 结构化路径兜底。
_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    r'(?i)(\b' + _SENSITIVE_NAME_PATTERN + r'\b["\']?\s*[:=]\s*)(["\'])(.*?)\2'
)
_UNQUOTED_ASSIGNMENT_PATTERN = re.compile(
    r'(?i)(\b' + _SENSITIVE_NAME_PATTERN + r'\b["\']?\s*[:=]\s*)([^\s,;}&"\']+)'
)
_AUTHORIZATION_VALUE_PATTERN = re.compile(
    r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]+'
)
_OPENAI_STYLE_SECRET_PATTERN = re.compile(r'\bsk-[A-Za-z0-9_-]+\b')
_URL_CREDENTIAL_PATTERN = re.compile(r'(?i)(https?://)[^/@\s]+@')
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


def _field_is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_FIELD_PATTERN.search(name)) and not _is_token_counter_field(name)


def _is_token_counter_field(name: str) -> bool:
    return bool(_TOKEN_COUNTER_FIELD_PATTERN.search(name))


def redact_sensitive_data(value: Any, *, known_values=(), mask: str = '***') -> Any:
    """Copy display data, masking secret fields and their occurrences in text."""
    secrets = {str(item) for item in known_values if item}

    def collect(item, secret=False):
        if isinstance(item, dict):
            for key, child in item.items():
                collect(child, secret or _field_is_sensitive(str(key)))
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child, secret)
        elif secret and isinstance(item, (str, int)) and item:
            secrets.add(str(item))

    def public(item):
        if isinstance(item, dict):
            return {key: (mask if child else '') if _field_is_sensitive(str(key))
                    else public(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [public(child) for child in item]
        if isinstance(item, str):
            for secret in ordered_secrets:
                item = item.replace(secret, mask)
            json_view = _redact_json_string(item, public)
            if json_view is not None:
                return json_view
            return redact_sensitive_text(item)
        return item

    collect(value)
    ordered_secrets = sorted(secrets, key=len, reverse=True)
    return public(value)


def _redact_json_string(value: str, public) -> str | None:
    """Redact strings that parse as a JSON object/array via the structured path.

    命中时返回重新序列化的脱敏文本；不是 JSON 或解析失败时返回 None，
    交回自由文本正则路径。整段字符串与 JSON 完全一致（允许首尾空白），
    避免把「错误信息里提到 JSON 片段」误判为结构化数据。
    """
    stripped = value.strip()
    if not stripped or stripped[0] not in '{[':
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    return json.dumps(public(parsed), ensure_ascii=False)


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
    redacted = _QUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: f'{match.group(1)}{match.group(2)}***REDACTED***{match.group(2)}',
        redacted,
    )
    return _UNQUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: f'{match.group(1)}***REDACTED***',
        redacted,
    )
