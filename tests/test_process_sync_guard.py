"""Cross-process sync exclusion and stale-lock recovery."""
import multiprocessing

from foundation.process_sync import release_sync_lock, sync_lock_held, try_acquire_sync_lock


def _check_from_child(path, results):
    descriptor = try_acquire_sync_lock(path)
    results.put(descriptor is None)
    if descriptor is not None:
        release_sync_lock(descriptor)


def test_sync_file_lock_blocks_another_process(tmp_path):
    lock = tmp_path / "sync.lock"
    descriptor = try_acquire_sync_lock(lock)
    assert descriptor is not None
    assert sync_lock_held(lock)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_check_from_child, args=(lock, queue))
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 0
    assert queue.get(timeout=5) is True
    release_sync_lock(descriptor)
    assert not sync_lock_held(lock)
    again = try_acquire_sync_lock(lock)
    assert again is not None
    release_sync_lock(again)


def test_sync_lock_released_when_owning_process_exits(tmp_path):
    lock = tmp_path / "sync.lock"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_exit_with_lock, args=(lock, queue))
    process.start()
    assert queue.get(timeout=10)
    process.join(timeout=15)
    assert process.exitcode == 0
    assert not sync_lock_held(lock)


def _exit_with_lock(path, results):
    descriptor = try_acquire_sync_lock(path)
    results.put(descriptor is not None)
    # Intentionally do not release: process death must clean up the lock.
    return
