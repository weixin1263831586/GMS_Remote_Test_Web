"""Regression coverage for the Redmine knowledge search route."""

import json
from types import SimpleNamespace

import pytest

from features.reports import analysis_api


@pytest.mark.asyncio
@pytest.mark.parametrize("hits", [[], [{"title": "Related historical case"}]])
async def test_knowledgebase_search_binds_keyword_only_limit(monkeypatch, hits):
    calls = []

    def search_similar(query, *, limit):
        calls.append((query, limit))
        return hits

    request = object()
    monkeypatch.setattr(analysis_api, "require_authenticated_user", lambda req: None)
    monkeypatch.setattr(
        analysis_api, "_get_knowledge_base",
        lambda req: SimpleNamespace(search_similar=search_similar),
    )

    response = await analysis_api.knowledgebase_search(request, query=" RAM ", limit=3)

    assert response.status_code == 200
    assert calls == [("RAM", 3)]
    assert json.loads(response.body)["data"] == {
        "query": "RAM", "results": hits, "count": len(hits),
    }
