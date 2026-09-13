"""FastAPI 路由自省工具(fastapi 0.141+ 兼容)。

fastapi 0.141 把 ``include_router`` 表示为惰性 ``_IncludedRouter`` 包装:
``app.routes`` 里只看得到包装对象(无 ``path``),旧式自省会漏掉全部
业务路由;且 0.141.1 对 ``APIWebSocketRoute`` 的 effective context
``path``/``methods`` 为空,真实值落在 ``starlette_route`` 上。

``flatten_app_routes`` 是统一的兼容展开入口,供契约快照、路由注册
断言与门禁测试使用。
"""

from __future__ import annotations

from typing import Any


def flatten_app_routes(app: Any) -> list[Any]:
    """展开 app.routes 为带 ``path``/``methods`` 的叶子路由列表。

    旧版 fastapi(无 _IncludedRouter)直接返回原列表,行为不变。
    """
    try:
        from fastapi.routing import _IncludedRouter
    except ImportError:  # pragma: no cover - 旧 fastapi 无该类型
        return list(app.routes)

    flattened: list[Any] = []
    for route in app.routes:
        if isinstance(route, _IncludedRouter):
            for context in route.effective_route_contexts():
                if not getattr(context, 'path', ''):
                    # fastapi 0.141.1: WebSocket context 的 path 为空,
                    # 真实 path 在 starlette_route 上。
                    starlette_route = getattr(context, 'starlette_route', None)
                    if starlette_route is not None and getattr(starlette_route, 'path', ''):
                        flattened.append(starlette_route)
                        continue
                if hasattr(context, 'path'):
                    flattened.append(context)
        elif hasattr(route, 'path'):
            flattened.append(route)
    return flattened


__all__ = ["flatten_app_routes"]
