from __future__ import annotations

from fastapi import FastAPI

from routers import ALL_ROUTERS
from routers.system import init_templates


def include_routes(app: FastAPI, templates) -> None:
    init_templates(templates)
    for router in ALL_ROUTERS:
        app.include_router(router)
