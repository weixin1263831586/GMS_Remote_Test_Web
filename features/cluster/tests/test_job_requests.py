"""Submission guards serialize different Web processes, including after exit."""

import multiprocessing

import pytest

from features.cluster.repository import ClusterRepository
from foundation.error_model import ApiError


def _try_guard(db_path, outcome):
    repo = ClusterRepository(db_path)
    try:
        with repo.job_request_guard("alice", "same-key"):
            outcome.put("acquired")
    except ApiError as exc:
        outcome.put(exc.code)


def test_submission_guard_is_process_safe(tmp_path):
    repo = ClusterRepository(tmp_path / "cluster.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    outcome = ctx.Queue()
    with repo.job_request_guard("alice", "same-key"):
        process = ctx.Process(target=_try_guard, args=(repo.db_path, outcome))
        process.start()
        try:
            assert outcome.get(timeout=10) == "STATE_CONFLICT"
            process.join(timeout=10)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    _try_guard(repo.db_path, outcome)
    assert outcome.get(timeout=2) == "acquired"
    outcome.close()


@pytest.mark.parametrize("key", ["", "spaces in key", "../path", "a" * 129])
def test_invalid_job_request_key_is_rejected(tmp_path, key):
    repo = ClusterRepository(tmp_path / "cluster.sqlite3")
    with pytest.raises(ApiError) as error, repo.job_request_guard("alice", key):
        pytest.fail("invalid key acquired a guard")
    assert error.value.code == "MALFORMED_REQUEST"
    assert "Idempotency-Key" in error.value.message
