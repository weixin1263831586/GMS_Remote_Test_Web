from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def success_response(
    data: Any = None,
    message: str | None = 'Success',
    **extra_fields,
) -> JSONResponse:
    content = {'success': True}
    if message is not None:
        content['message'] = message
    if data is not None:
        content['data'] = data
    content.update(extra_fields)
    return JSONResponse(content=content)


def error_response(
    error: str,
    status_code: int = 500,
    detail: Any = None,
    **extra_fields,
) -> JSONResponse:
    content = {'success': False, 'error': error}
    if detail is not None:
        content['detail'] = detail
    content.update(extra_fields)
    return JSONResponse(content=content, status_code=status_code)
