from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

from foundation.cache import TTLCache
from workflows import cluster_test_execution as workflow


def test_module_cache_normalizes_paths_isolates_results_and_expires(tmp_path, monkeypatch):
    cache = TTLCache(30, max_entries=32)
    monkeypatch.setattr(workflow, '_suite_modules_cache', cache)
    now = [100.0]
    monkeypatch.setattr('foundation.cache.time.monotonic', lambda: now[0])
    (tmp_path / 'First.config').touch()
    first = workflow._list_suite_modules(str(tmp_path))
    first.clear()
    (tmp_path / 'Second.config').touch()
    assert set(workflow._list_suite_modules(str(tmp_path) + '/.')) == {'First'}
    now[0] += 31
    assert set(workflow._list_suite_modules(str(tmp_path))) == {'First', 'Second'}


def test_concurrent_module_checks_scan_once(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, '_suite_modules_cache', TTLCache(30, max_entries=32))
    (tmp_path / 'First.config').touch()
    scan = Mock(wraps=workflow._scan_suite_modules)
    monkeypatch.setattr(workflow, '_scan_suite_modules', scan)
    barrier = Barrier(8)

    def read(_):
        barrier.wait(timeout=5)
        return workflow._list_suite_modules(str(tmp_path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(8)))
    assert all('First' in result for result in results)
    assert scan.call_count == 1
