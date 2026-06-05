"""统一的API错误处理装饰器。"""

import asyncio
import logging
from functools import wraps

from fastapi import HTTPException

from core.api_response import error_response

logger = logging.getLogger(__name__)


def handle_api_errors(func):
    """统一API错误处理装饰器 - 支持同步和异步函数"""
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            return error_response(str(e), status_code=500)

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            return error_response(str(e), status_code=500)

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper
