"""邮件地址/文本解析：发送管线与配置层共用的纯函数。

无 IO、无配置读取；``None`` 返回值统一表示「输入非法」，供管线
校验阶段判定，不抛异常（与历史 send_email 返回契约一致）。
"""

from __future__ import annotations

import re
from typing import Any


_SAFE_ADDRESS_RE = re.compile(r'[^\s,;<>"@]+@[^\s,;<>"@]+')


def split_emails(email_input: str | list[str] | None) -> list[str]:
    """将输入（字符串或列表）转换为干净的邮件地址列表。

    支持逗号、分号分隔；递归处理列表内的混合分隔符。来自 rk-email send_email.py。
    """
    if not email_input:
        return []

    if isinstance(email_input, list):
        # 递归处理列表中的每一项，防止项内含有逗号
        result: list[str] = []
        for item in email_input:
            result.extend(split_emails(item))
        return result

    raw_list = email_input.replace(';', ',').split(',')
    return [e.strip() for e in raw_list if e.strip()]


def normalize_email_addresses(
    email_input: str | list[str] | None,
) -> list[str] | None:
    """Split and validate envelope/header addresses; ``None`` means invalid."""
    if email_input is None:
        return []
    if not isinstance(email_input, (str, list)):
        return None
    if isinstance(email_input, list) and not all(
        isinstance(item, str) for item in email_input
    ):
        return None
    addresses = split_emails(email_input)
    if any(not _SAFE_ADDRESS_RE.fullmatch(address) for address in addresses):
        return None
    return addresses


def safe_header_text(value: Any) -> str:
    """Collapse CR/LF and other folding whitespace before MIME headers."""
    return ' '.join(str(value or '').split())
