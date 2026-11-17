"""Router failures hide internal data while retaining a usable log reference."""

import re
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from features.assistant.executor import ActionExecutor
from features.assistant.route_invocation import call_router_function


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exception', 'business'])
async def test_router_fetch_redacts_failures(monkeypatch, caplog, failure):
    module = ModuleType('_assistant_router_error_fixture')
    router = APIRouter()

    @router.get('/query')
    async def query():
        if failure == 'exception':
            raise RuntimeError('password=isolated-private-value path=/private/internal')
        return JSONResponse({'success': False, 'error': 'password=isolated-private-value'}, status_code=502)

    module.query = query
    module.router = router
    monkeypatch.setitem(sys.modules, module.__name__, module)
    app = FastAPI()
    app.include_router(router)

    result, payload = await ActionExecutor._fetch_router_json(
        module.__name__, 'query', request=SimpleNamespace(app=app), api_path='/query', method='GET',
    )

    assert payload == {}
    assert not result.success
    assert 'isolated-private-value' not in result.error + result.formatted_text
    assert '/private/internal' not in result.error + result.formatted_text
    if failure == 'exception':
        request_id = re.search(r'request_id=([a-f0-9]{12})', result.error).group(1)
        assert any(request_id in record.getMessage() and record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_unresolved_executor_keeps_request_id_in_the_error_log(monkeypatch, caplog):
    module = ModuleType('_assistant_missing_executor_fixture')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    tool = SimpleNamespace(name='fixture_query', executor_ref=f'{module.__name__}:missing')

    result = await call_router_function(tool, None, None, {})

    assert not result.success
    assert module.__name__ not in result.error + result.formatted_text
    request_id = re.search(r'request_id=([a-f0-9]{12})', result.error).group(1)
    assert any(request_id in record.getMessage() and record.exc_info for record in caplog.records)
