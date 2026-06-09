"""Shared API help text generation for routers."""

from fastapi.responses import PlainTextResponse


def generate_help_or_continue(help_flag: bool, method: str, path: str):
    """当 help_flag 为 True 时，生成 API 帮助文本并返回 PlainTextResponse；否则返回 None。"""
    if not help_flag:
        return None
    try:
        from routers.system import generate_per_api_help_text
        help_text = generate_per_api_help_text(method, path)
    except ImportError:
        return None
    if help_text:
        return PlainTextResponse(
            content=help_text,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=300"},
        )
    return None
