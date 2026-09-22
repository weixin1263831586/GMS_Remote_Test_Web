"""android-internals-wiki frontmatter 解析（untrusted data 投影）。

上游 frontmatter 是 untrusted evidence（ADR 0014）：这里只做数据投影，
永不把它当指令执行；``yaml.safe_load`` 不执行任意构造器。ADR 0014 要求
完整解析（tags/sources 等数组与嵌套结构保留），YAML 不可用时回退到
纯文本标量投影，保证正文仍可索引。
"""

from __future__ import annotations

import logging
import re
from typing import Any


logger = logging.getLogger(__name__)

_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _scalarize(value: Any) -> str:
    """YAML 标量投影：str 原样、list/数字/日期转稳定展示串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return _strip_quotes(value).strip()
    if isinstance(value, (list, tuple)):
        return "; ".join(_scalarize(item) for item in value if _scalarize(item))
    return str(value).strip()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """安全 YAML frontmatter 解析：保留 tags/sources 等数组与嵌套结构。

    此前只解析顶层标量，把 sources（Wiki→codesearch 的关键
    连接）和 tags 直接丢掉。YAML 解析失败时回退到纯文本标量投影，保证
    正文仍可索引——坏 frontmatter 不丢正文。
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block = text[3:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        import yaml  # 可选依赖，缺失时走标量回退

        loaded = yaml.safe_load(block)
        if isinstance(loaded, dict):
            return loaded, body
        return {}, body
    except ImportError:
        pass
    except Exception:  # yaml.YAMLError 等：不因坏 frontmatter 丢正文
        logger.debug("frontmatter YAML parse failed; falling back to scalars")
    scalars: dict[str, Any] = {}
    for line in block.splitlines():
        match = _FM_KEY_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if not value.strip() or value.lstrip().startswith(("- ", "[")):
            continue  # 列表/嵌套值：回退模式下不猜结构
        scalars[key] = _strip_quotes(value)
    return scalars, body
