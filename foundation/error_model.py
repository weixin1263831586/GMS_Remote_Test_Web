"""统一 API 错误模型。

全项目唯一错误码定义。基础设施故障（SSH/Worker/adb/后端服务）不得映射成
500 —— 500 仅表示未预期的程序错误，否则调用方（前端 / Agent）无法区分
"重试 / 修正参数 / 询问用户 / 停止"。

失败响应统一载荷（与既有 JSON envelope 兼容，仅追加字段）：

    {"success": false, "error": <message>, "code": <CODE>,
     "next_actions": [{"action": "...", "command": "..."}, ...]}

用法一：路由内直接构造

    raise ApiError.upstream_failure(
        "SSH connection failed", service="ssh",
        next_actions=[{"action": "check host sshd"}],
    )

用法二：经 @handle_api_errors 包装的路由，抛出的 ApiError 会被原样转成
带错误码的 envelope；未预期的异常仍回落 500。

应用启动时调用 register_api_error_handler(app) 注册全局兜底处理器，
覆盖未使用装饰器的路由。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# 错误码表：code → HTTP status。唯一真源，路由不得自造映射。
API_ERROR_CODES: dict[str, int] = {
    'MALFORMED_REQUEST': 400,
    'UNAUTHENTICATED': 401,
    'FORBIDDEN': 403,
    'NOT_FOUND': 404,
    'STATE_CONFLICT': 409,
    'INVALID_SEMANTICS': 422,
    'INTERNAL_ERROR': 500,
    'UPSTREAM_FAILURE': 502,
    'DEPENDENCY_UNAVAILABLE': 503,
    'DEPENDENCY_TIMEOUT': 504,
}


@dataclass(frozen=True)
class ApiError(Exception):
    """带全局错误码的业务/基础设施异常。"""

    code: str
    message: str
    details: Any = None
    next_actions: tuple[dict[str, Any], ...] = field(default=())

    def __post_init__(self) -> None:
        if self.code not in API_ERROR_CODES:
            raise ValueError(
                f'Unknown API error code: {self.code!r}; '
                f'use foundation.error_model.API_ERROR_CODES'
            )

    @property
    def status_code(self) -> int:
        return API_ERROR_CODES[self.code]

    def envelope(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'success': False,
            'error': self.message,
            'code': self.code,
        }
        if self.details is not None:
            payload['details'] = self.details
        if self.next_actions:
            payload['next_actions'] = list(self.next_actions)
        return payload

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            content=self.envelope(),
            status_code=self.status_code,
        )

    # ---- 语义化构造器：新增代码必须走这里，禁止散落裸状态码 ----

    @classmethod
    def malformed_request(cls, message: str, **kwargs: Any) -> ApiError:
        return cls(code='MALFORMED_REQUEST', message=message, **kwargs)

    @classmethod
    def unauthenticated(cls, message: str = 'Authentication required', **kwargs: Any) -> ApiError:
        return cls(code='UNAUTHENTICATED', message=message, **kwargs)

    @classmethod
    def forbidden(cls, message: str = 'Insufficient permission', **kwargs: Any) -> ApiError:
        return cls(code='FORBIDDEN', message=message, **kwargs)

    @classmethod
    def not_found(cls, message: str, **kwargs: Any) -> ApiError:
        return cls(code='NOT_FOUND', message=message, **kwargs)

    @classmethod
    def conflict(cls, message: str, **kwargs: Any) -> ApiError:
        return cls(code='STATE_CONFLICT', message=message, **kwargs)

    @classmethod
    def invalid_semantics(cls, message: str, **kwargs: Any) -> ApiError:
        return cls(code='INVALID_SEMANTICS', message=message, **kwargs)

    @classmethod
    def internal(cls, message: str, **kwargs: Any) -> ApiError:
        return cls(code='INTERNAL_ERROR', message=message, **kwargs)

    @classmethod
    def upstream_failure(
        cls, message: str, *, service: str | None = None, **kwargs: Any
    ) -> ApiError:
        """Worker / SSH / adb / 远端后端的执行失败（可提示重试）。"""
        details = kwargs.pop('details', None)
        if service and details is None:
            details = {'service': service}
        return cls(
            code='UPSTREAM_FAILURE', message=message, details=details, **kwargs
        )

    @classmethod
    def dependency_unavailable(cls, message: str, **kwargs: Any) -> ApiError:
        return cls(code='DEPENDENCY_UNAVAILABLE', message=message, **kwargs)

    @classmethod
    def dependency_timeout(cls, message: str, **kwargs: Any) -> ApiError:
        return cls(code='DEPENDENCY_TIMEOUT', message=message, **kwargs)


def register_api_error_handler(app: FastAPI) -> None:
    """把 ApiError 兜底处理器挂到应用上（create_app 统一调用一次）。"""

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return exc.to_response()
