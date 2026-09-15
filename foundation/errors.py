from __future__ import annotations

import asyncio
import logging
from functools import wraps

from fastapi import HTTPException

from foundation.error_model import ApiError
from foundation.responses import error_response


logger = logging.getLogger(__name__)


def handle_api_errors(function):
    @wraps(function)
    async def async_wrapper(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except HTTPException:
            raise
        except ApiError as exc:
            # 统一错误码模型（foundation/error_model.py）：基础设施失败
            # 按 code 映射 4xx/5xx，不再一律落 500。
            logger.info(
                'API error in %s: %s %s', function.__name__, exc.code, exc.message
            )
            return exc.to_response()
        except Exception as exc:
            logger.exception('Error in %s', function.__name__)
            return error_response(str(exc), status_code=500)

    @wraps(function)
    def sync_wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except HTTPException:
            raise
        except ApiError as exc:
            logger.info(
                'API error in %s: %s %s', function.__name__, exc.code, exc.message
            )
            return exc.to_response()
        except Exception as exc:
            logger.exception('Error in %s', function.__name__)
            return error_response(str(exc), status_code=500)

    return async_wrapper if asyncio.iscoroutinefunction(function) else sync_wrapper
