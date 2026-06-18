from __future__ import annotations

import asyncio
import logging
from functools import wraps

from fastapi import HTTPException

from foundation.responses import error_response


logger = logging.getLogger(__name__)


def handle_api_errors(function):
    @wraps(function)
    async def async_wrapper(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception('Error in %s', function.__name__)
            return error_response(str(exc), status_code=500)

    @wraps(function)
    def sync_wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception('Error in %s', function.__name__)
            return error_response(str(exc), status_code=500)

    return async_wrapper if asyncio.iscoroutinefunction(function) else sync_wrapper
