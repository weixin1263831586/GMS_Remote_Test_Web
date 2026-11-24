"""Regression coverage for the native Controller smoke route inventory."""

import pytest
from fastapi import APIRouter, FastAPI

from scripts.release import compiled_entry


def test_smoke_routes_expand_included_routers_and_reject_incomplete_apps():
    def endpoint():
        return {}

    required = {"/api/auth/status", "/api/auth/setup", *compiled_entry.REQUIRED_PAGE_ROUTES}

    def build_app(paths):
        app = FastAPI()
        outer = APIRouter()
        inner = APIRouter()
        for path in sorted(paths):
            inner.add_api_route(path, endpoint)
        outer.include_router(inner)
        app.include_router(outer)
        return app

    filler = {f"/extra/{index}" for index in range(30)}
    assert required.issubset(compiled_entry._validate_smoke_routes(build_app(required | filler)))
    with pytest.raises(RuntimeError, match="/api/auth/setup"):
        compiled_entry._validate_smoke_routes(build_app((required - {"/api/auth/setup"}) | filler))
    with pytest.raises(RuntimeError, match="minimum 30"):
        compiled_entry._validate_smoke_routes(build_app(required))
