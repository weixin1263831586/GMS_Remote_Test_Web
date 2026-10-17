import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from foundation.cache import TTLCache


def test_lru_capacity_and_expired_entries_are_pruned(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('foundation.cache.time.monotonic', lambda: now[0])
    cache = TTLCache(10, max_entries=2)
    cache.set('a', 1)
    cache.set('b', 2)
    assert cache.get('a') == 1
    cache.set('c', 3)
    assert cache.get('b') is None
    now[0] += 10
    cache.set('d', 4)
    assert list(cache._items) == ['d']


def test_concurrent_cold_loads_are_coalesced():
    cache = TTLCache(10, max_entries=2)
    barrier = Barrier(8)
    calls = []

    def load():
        calls.append(True)
        return 'value'

    def read(_):
        barrier.wait(timeout=5)
        return cache.get_or_load('key', load)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(read, range(8))) == ['value'] * 8
    assert len(calls) == 1


def test_clear_prevents_slow_sync_load_from_repopulating_cache():
    cache = TTLCache(10)
    started, release = Event(), Event()

    def load():
        started.set()
        assert release.wait(timeout=5)
        return 'old'

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cache.get_or_load, 'key', load)
        try:
            assert started.wait(timeout=5)
            cache.clear()
        finally:
            release.set()
        assert future.result(timeout=5) == 'old'
    assert cache.get('key') is None


def test_async_loads_coalesce_and_waiter_cancellation_does_not_cancel_load():
    async def scenario():
        cache = TTLCache(10)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def load():
            calls.append(True)
            started.set()
            await release.wait()
            return 'value'

        first = asyncio.create_task(cache.get_or_load_async('key', load))
        await started.wait()
        second = asyncio.create_task(cache.get_or_load_async('key', load))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == 'value'
        assert await cache.get_or_load_async('key', load) == 'value'
        assert len(calls) == 1

    asyncio.run(scenario())


def test_failed_async_load_is_retried_and_clear_fences_inflight_result():
    async def scenario():
        cache = TTLCache(10)

        async def fail():
            raise ValueError('dependency failed')

        with pytest.raises(ValueError):
            await cache.get_or_load_async('key', fail)
        started, release = asyncio.Event(), asyncio.Event()

        async def old():
            started.set()
            await release.wait()
            return 'old'

        async def new():
            return 'new'

        previous = asyncio.create_task(cache.get_or_load_async('key', old))
        await started.wait()
        cache.clear()
        assert await cache.get_or_load_async('key', new) == 'new'
        release.set()
        assert await previous == 'old'
        assert cache.get('key') == 'new'

    asyncio.run(scenario())


def test_disabled_cache_does_not_retain_values():
    cache = TTLCache(0)
    cache.set('key', 'value')
    assert not cache._items


def test_invalidating_another_key_keeps_sync_load_cacheable():
    cache = TTLCache(10)
    started, release = Event(), Event()

    def load():
        started.set()
        assert release.wait(timeout=5)
        return 'B-value'

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cache.get_or_load, 'B', load)
        try:
            assert started.wait(timeout=5)
            cache.invalidate('A')
        finally:
            release.set()
        assert future.result(timeout=5) == 'B-value'
    assert cache.get('B') == 'B-value'
    assert not cache._key_versions


def test_async_invalidation_affects_only_its_key():
    async def scenario():
        cache = TTLCache(10)
        started, release = asyncio.Event(), asyncio.Event()

        async def load():
            started.set()
            await release.wait()
            return 'value'

        task = asyncio.create_task(cache.get_or_load_async('B', load))
        await started.wait()
        cache.invalidate('A')
        release.set()
        assert await task == 'value'
        assert cache.get('B') == 'value'
        started.clear()
        release.clear()
        task = asyncio.create_task(cache.get_or_load_async('C', load))
        await started.wait()
        cache.invalidate('C')
        release.set()
        assert await task == 'value'
        assert cache.get('C') is None
        assert not cache._key_versions

    asyncio.run(scenario())


def test_mixed_sync_async_interleave_keeps_newest_started_load():
    """同步/异步对同一键交错完成时，后启动的加载持有缓存槽位（外部评审 P2）。"""
    cache = TTLCache(10)

    async def scenario():
        loop = asyncio.get_running_loop()
        async_started, async_release = asyncio.Event(), asyncio.Event()
        sync_started, sync_release = Event(), Event()

        async def async_load():
            async_started.set()
            await async_release.wait()
            return 'old'

        async_task = asyncio.create_task(cache.get_or_load_async('key', async_load))
        await async_started.wait()

        def sync_load():
            sync_started.set()
            assert sync_release.wait(5)
            return 'new'

        with ThreadPoolExecutor(max_workers=1) as pool:
            sync_future = pool.submit(cache.get_or_load, 'key', sync_load)
            await loop.run_in_executor(None, sync_started.wait, 5)
            # 同步加载（后启动）先完成并写入；异步（先启动）最后完成。
            sync_release.set()
            assert sync_future.result(5) == 'new'
            async_release.set()
            assert await async_task == 'old'
        assert cache.get('key') == 'new'
        assert not cache._key_versions
        assert not cache._load_seq

    asyncio.run(scenario())


def test_reverse_mixed_interleave_also_keeps_newest_started_load():
    """先同步后异步交错完成，同样是后启动的异步加载持有缓存槽位。"""
    cache = TTLCache(10)
    sync_started, sync_release = Event(), Event()

    def sync_load():
        sync_started.set()
        assert sync_release.wait(5)
        return 'old'

    async def scenario():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            sync_future = pool.submit(cache.get_or_load, 'key', sync_load)
            await loop.run_in_executor(None, sync_started.wait, 5)
            async_started, async_release = asyncio.Event(), asyncio.Event()

            async def async_load():
                async_started.set()
                await async_release.wait()
                return 'new'

            async_task = asyncio.create_task(cache.get_or_load_async('key', async_load))
            await async_started.wait()
            # 先启动的同步加载最后完成，只回调用方、不回写缓存。
            sync_release.set()
            assert sync_future.result(5) == 'old'
            async_release.set()
            assert await async_task == 'new'
        assert cache.get('key') == 'new'

    asyncio.run(scenario())
