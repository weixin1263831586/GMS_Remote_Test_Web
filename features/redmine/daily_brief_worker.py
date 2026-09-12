"""Standalone durable worker for Web-triggered Redmine Daily Brief jobs."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from foundation.config import settings

from .daily_brief_repository import TERMINAL_RUN_STATUSES, DailyBriefRepository
from .daily_brief_service import DailyBriefService
from .users import _now


logger = logging.getLogger("daily_brief_worker")
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_LEASE_SECONDS = 90


def _service_for_owner(owner_id: str) -> DailyBriefService:
    from .api import get_redmine_service_for_owner

    redmine = get_redmine_service_for_owner(owner_id)
    return DailyBriefService(owner_id, config_manager=redmine.agent.config_manager)


def discover_repositories(data_root: Path | None = None) -> list[DailyBriefRepository]:
    root = Path(data_root or settings.data_root) / "redmine" / "by_user"
    if not root.is_dir():
        return []
    return [
        DailyBriefRepository(owner_dir)
        for owner_dir in sorted(root.iterdir())
        if owner_dir.is_dir() and (owner_dir / "daily_brief.sqlite3").is_file()
    ]


async def _execute_job(
    repository: DailyBriefRepository,
    job: dict[str, Any],
    service_factory: Callable[[str], DailyBriefService],
) -> None:
    run = repository.get_run(str(job["run_id"]))
    if run is None:
        raise ValueError(f"daily brief run not found: {job['run_id']}")
    service = service_factory(run.owner_id)
    if job["kind"] == "run":
        completed = await service.execute_run(run.run_id)
        if completed is None:
            raise RuntimeError(f"daily brief run disappeared: {run.run_id}")
        return

    run.status = "analyzing"
    run.error = ""
    run.finished_at = ""
    service.repository.update_run(run)
    result = await service.reanalyze_issue(
        run.brief_date, int(job.get("issue_id") or 0)
    )
    if result.get("error"):
        raise RuntimeError(str(result["error"]))


async def _heartbeat(
    repository: DailyBriefRepository,
    job_id: str,
    worker_id: str,
    lease_seconds: int,
) -> None:
    interval = max(2.0, lease_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        if not repository.renew_job(job_id, worker_id, lease_seconds):
            raise RuntimeError(f"lost lease for daily brief job {job_id}")


async def run_claimed_job(
    repository: DailyBriefRepository,
    job: dict[str, Any],
    worker_id: str,
    stop_event: asyncio.Event,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    service_factory: Callable[[str], DailyBriefService] = _service_for_owner,
) -> bool:
    """Run one claimed job. Return False when shutdown should stop the loop."""
    job_id = str(job["job_id"])
    work = asyncio.create_task(_execute_job(repository, job, service_factory))
    heartbeat = asyncio.create_task(
        _heartbeat(repository, job_id, worker_id, lease_seconds)
    )
    stopping = asyncio.create_task(stop_event.wait())
    done, _ = await asyncio.wait(
        {work, heartbeat, stopping}, return_when=asyncio.FIRST_COMPLETED
    )
    if stopping in done and stop_event.is_set() and not work.done():
        work.cancel()
        with suppress(asyncio.CancelledError):
            await work
        repository.requeue_job(
            job_id, worker_id, reason="analysis worker stopped; queued for retry"
        )
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        return False

    if heartbeat in done and not work.done():
        work.cancel()
        with suppress(asyncio.CancelledError):
            await work
        try:
            await heartbeat
        except Exception as exc:
            logger.error("daily brief job %s stopped after lease loss: %s", job_id, exc)
        stopping.cancel()
        with suppress(asyncio.CancelledError):
            await stopping
        return True

    stopping.cancel()
    heartbeat.cancel()
    with suppress(asyncio.CancelledError):
        await stopping
    with suppress(asyncio.CancelledError):
        await heartbeat
    try:
        await work
    except Exception as exc:
        logger.exception("daily brief job %s failed", job_id)
        repository.finish_job(job_id, worker_id, error=str(exc))
        run = repository.get_run(str(job["run_id"]))
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            run.status = "failed"
            run.error = str(exc)[:1000]
            run.finished_at = _now()
            repository.update_run(run)
    else:
        repository.finish_job(job_id, worker_id)
    return True


async def worker_loop(
    stop_event: asyncio.Event,
    *,
    data_root: Path | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    service_factory: Callable[[str], DailyBriefService] = _service_for_owner,
) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    logger.info("daily brief worker started as %s", worker_id)
    while not stop_event.is_set():
        claimed: tuple[DailyBriefRepository, dict[str, Any]] | None = None
        for repository in discover_repositories(data_root):
            job = repository.claim_next_job(worker_id, lease_seconds)
            if job is not None:
                claimed = repository, job
                break
        if claimed is not None:
            if not await run_claimed_job(
                *claimed,
                worker_id,
                stop_event,
                lease_seconds=lease_seconds,
                service_factory=service_factory,
            ):
                break
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.1, poll_seconds))
        except asyncio.TimeoutError:
            pass
    logger.info("daily brief worker stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daily_brief_worker")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def run() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop_event.set)
        await worker_loop(
            stop_event,
            poll_seconds=max(0.1, args.poll_seconds),
            lease_seconds=max(10, args.lease_seconds),
        )

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
