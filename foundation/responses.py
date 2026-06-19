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


class ApiResponse:
    @staticmethod
    def success(data=None, message="操作成功"):
        return success_response(data=data, message=message)

    @staticmethod
    def error(error_message, status_code=500, **extra_fields):
        return error_response(error=error_message, status_code=status_code, **extra_fields)

    @staticmethod
    def device_results(results, operation_name):
        success_count = sum(result.get('success', False) for result in results)
        fail_count = len(results) - success_count
        return ApiResponse.success(
            {
                'results': results,
                'summary': {
                    'total': len(results),
                    'success': success_count,
                    'failed': fail_count,
                },
            },
            f"{operation_name}完成: 成功 {success_count} 台, 失败 {fail_count} 台",
        )
