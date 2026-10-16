"""Do not let asynchronous inventory probes outlive a test's subprocess mocks."""

import pytest

from worker_agent import device_actions


def _stop_inventory_probe():
    device_actions._details_enrich_stop.set()
    thread = device_actions._details_enrich_thread
    if thread is not None:
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError('Inventory enrichment did not stop between tests')
    device_actions._DEVICE_DETAILS_CACHE.clear()
    device_actions._details_refresh_at.clear()
    device_actions._details_last_seen_at.clear()


@pytest.fixture(autouse=True)
def isolated_inventory_background_worker():
    _stop_inventory_probe()
    yield
    _stop_inventory_probe()
