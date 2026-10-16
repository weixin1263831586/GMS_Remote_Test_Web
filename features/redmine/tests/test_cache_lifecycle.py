import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from features.redmine import client as client_module
from features.redmine.api import _check_ttl_cache, _update_ttl_cache


@pytest.fixture(autouse=True)
def clear_assignee_caches():
    client_module._ASSIGNEE_COUNT_CACHE.clear()
    client_module.clear_historical_trend_cache()
    yield
    client_module._ASSIGNEE_COUNT_CACHE.clear()
    client_module.clear_historical_trend_cache()


def test_each_owner_retains_its_own_ttl_and_returned_values_are_isolated():
    cache = {}
    data = {'rows': [{'count': 1}]}
    _update_ttl_cache(cache, 'long', 1000, data, ttl=3600)
    data['rows'][0]['count'] = 99
    _update_ttl_cache(cache, 'short', 1002, {}, ttl=1)
    result = _check_ttl_cache(cache, 'long', 3600, 1003)
    assert result == {'rows': [{'count': 1}]}
    result['rows'][0]['count'] = 77
    assert _check_ttl_cache(cache, 'long', 3600, 1003)['rows'][0]['count'] == 1
    assert _check_ttl_cache(cache, 'short', 1, 1003) is None
    _update_ttl_cache(cache, 'disabled', 1004, {}, ttl=0)
    assert 'disabled' not in cache
    assert 'short' not in cache
    assert 'long' in cache
    assert _check_ttl_cache(cache, 'long', 1, 1004) is None


def test_stats_cache_capacity_keeps_newest_entries():
    cache = {}
    for index in range(300):
        _update_ttl_cache(cache, str(index), index, {}, ttl=3600)
    assert len(cache) == 256
    assert '0' not in cache
    assert '299' in cache


def fake_client(owner='review'):
    client = object.__new__(client_module.RedmineClient)
    client._cache_owner_key = owner
    return client


def test_daily_cutoff_rollover_keeps_boundary_issue(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 10, 16, 12)

        @classmethod
        def now(cls):
            return cls.current

    monkeypatch.setattr(client_module, 'datetime', Clock)
    client = fake_client()
    boundary = Clock.current - timedelta(days=180)
    client.fetch_issues_by_assignee = AsyncMock(return_value=[SimpleNamespace(closed_on=boundary)])

    async def scenario():
        before = await client.resolved_trends_by_assignee(123)
        Clock.current += timedelta(days=1)
        # The recent TTL expires while the seven-day history remains live.
        client_module._ASSIGNEE_TREND_CACHE.clear()
        after = await client.resolved_trends_by_assignee(123)
        assert before['resolved_daily'] == after['resolved_daily'] == [
            {'date': boundary.date().isoformat(), 'count': 1},
        ]

    asyncio.run(scenario())


def test_trend_limit_freshness_and_owner_are_separate_cache_keys():
    first = fake_client('first')
    second = fake_client('second')

    async def fetch(**kwargs):
        return [SimpleNamespace(closed_on=datetime.now() - timedelta(days=20))] * kwargs['limit']

    first.fetch_issues_by_assignee = AsyncMock(side_effect=fetch)
    second.fetch_issues_by_assignee = AsyncMock(return_value=[])

    async def scenario():
        small = await first.resolved_trends_by_assignee(1, limit=1)
        large = await first.resolved_trends_by_assignee(1, limit=2)
        assert small['resolved_daily'][0]['count'] == 1
        assert large['resolved_daily'][0]['count'] == 2
        changed = await first.resolved_trends_by_assignee(1, freshness_days=1, limit=2)
        assert changed['resolved_daily'][0]['count'] == 2
        assert first.fetch_issues_by_assignee.await_count == 6
        assert (await second.resolved_trends_by_assignee(1, limit=1))['resolved_daily'] == []
        large['resolved_daily'][0]['count'] = 999
        repeated = await first.resolved_trends_by_assignee(1, limit=2)
        assert repeated['resolved_daily'][0]['count'] == 2

    asyncio.run(scenario())


def test_concurrent_trend_loads_coalesce_and_failed_segments_are_not_cached():
    client = fake_client()
    client.fetch_issues_by_assignee = AsyncMock(side_effect=RuntimeError('upstream unavailable'))

    async def scenario():
        assert (await client.resolved_trends_by_assignee(1))['resolved_daily'] == []
        assert (await client.resolved_trends_by_assignee(1))['resolved_daily'] == []
        assert client.fetch_issues_by_assignee.await_count == 4
        client.fetch_issues_by_assignee = AsyncMock(return_value=[SimpleNamespace(closed_on=datetime.now())])
        results = await asyncio.gather(*(client.resolved_trends_by_assignee(1) for _ in range(8)))
        assert all(result['resolved_daily'][0]['count'] == 1 for result in results)
        assert client.fetch_issues_by_assignee.await_count == 2

    asyncio.run(scenario())


def test_concurrent_counts_share_the_upstream_queries_and_isolate_results():
    class Result(list):
        total_count = 3

    client = fake_client()
    client._redmine = SimpleNamespace(issue=SimpleNamespace(filter=Mock(return_value=Result())))

    async def scenario():
        results = await asyncio.gather(*(client.count_issues_by_assignee(1) for _ in range(8)))
        assert client._redmine.issue.filter.call_count == 2
        results[0]['total_owned'] = 999
        assert (await client.count_issues_by_assignee(1))['total_owned'] == 3

    asyncio.run(scenario())
