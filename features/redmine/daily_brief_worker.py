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


class OwnerFairnessCursor:
    """跨 owner 的 round-robin 游标(P2 fairness)。

    旧模型每轮都从字母序第一个 owner 目录开始 claim:owner A 长期有
    大量耗时 AI 任务时,owner B/C 会被明显饿死。游标记住上一轮服务的
    owner 起始偏移,让每个 owner 轮流成为扫描起点。

    单 Worker 进程内有效;多 Worker 部署下各自独立轮转(仍然公平:
    任意 Worker 都不再固定偏向字母序靠前的 owner)。
    """

    def __init__(self) -> None:
        self._offset = 0

    def rotate(self, items: list[DailyBriefRepository]) -> list[DailyBriefRepository]:
        if not items:
            return items
        offset = self._offset % len(items)
        self._offset = (self._offset + 1) % len(items)
        return items[offset:] + items[:offset]


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

    # issue-job 的前置状态写只在 run 处于非执行态时进行；claim 门控已
    # 保证无活跃 run-job，这里再防御一次（lease 恢复窗口内 run-job 可能
    # 刚被另一 Worker 领取），避免把执行中的 run 状态打回 analyzing。
    if run.status in ("pending", "failed", "cancelled"):
        run.status = "analyzing"
        run.error = ""
        run.finished_at = ""
        service.repository.update_run(run)
    result = await service.reanalyze_issue(
        run.brief_date, int(job.get("issue_id") or 0), run_id=run.run_id
    )
    if result.get("error"):
        raise RuntimeError(str(result["error"]))


async def _heartbeat(
    repository: DailyBriefRepository,
    job_id: str,
    worker_id: str,
    lease_token: str,
    lease_seconds: int,
) -> None:
    interval = max(2.0, lease_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        if not repository.renew_job(job_id, worker_id, lease_token, lease_seconds):
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
    """Run one claimed job. Return False when shutdown should stop the loop.

    所有归属操作（renew/requeue/finish）都携带 claim 时拿到的
    lease_token 做 CAS：租约被其他 Worker 接管后，本 Worker 的迟到
    写入会被安全拒绝，而不是覆盖新状态。

    状态收敛兜底（finally）：无论 work/heartbeat 哪条路径异常退出，只要
    job 仍处于本租约的 running 状态，就立即回到 queued 等待重试，而不是
    等待下一次 lease expiration 才恢复。
    """
    job_id = str(job["job_id"])
    lease_token = str(job.get("lease_token") or "")
    outcome: str | None = None  # None=未决, "done"=已 finish, "requeued"
    try:
        work = asyncio.create_task(_execute_job(repository, job, service_factory))
        heartbeat = asyncio.create_task(
            _heartbeat(repository, job_id, worker_id, lease_token, lease_seconds)
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
                job_id, worker_id, lease_token,
                reason="analysis worker stopped; queued for retry",
            )
            outcome = "requeued"
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            return False

        if heartbeat in done and not work.done():
            # 心跳先退出（lease 丢失或心跳自身异常）：取消 work,但不做
            # 任何归属写入——lease_token CAS 会拒绝迟到写入,任务由
            # claim_next_job() 的过期恢复路径重新排队。
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
            try:
                await heartbeat
            except Exception as exc:
                logger.error("daily brief job %s stopped after lease loss: %s", job_id, exc)
            else:
                # 心跳正常返回(不应发生)也按租约丢失处理。
                logger.error("daily brief job %s heartbeat exited early", job_id)
            outcome = "lease-lost"
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
            finished = repository.finish_job(
                job_id, worker_id, lease_token, error=str(exc)
            )
            outcome = "done" if finished else "lease-lost"
            # 只有仍持有租约的 Worker 才能收敛 run。若 finish_job 的 CAS
            # 失败，任务已被其他 Worker 接管；此时旧 Worker 再写 run
            # 会覆盖新执行者的 pending/analyzing/completed 状态。
            if finished:
                run = repository.get_run(str(job["run_id"]))
                # issue-job 失败只影响该 issue 行，不拥有 run 级终态
                # 收敛权（审核意见 P1）：排队的 issue-job 与 force 重跑
                # 的 run-job 并存时，issue 行可能已被 run 重置删除，
                # 这类失败若写 run 会把刚重置的执行打成 failed。
                # run 终态收敛只属于 run-job（或人工取消）。
                if run is not None and job["kind"] == "run" and run.status not in TERMINAL_RUN_STATUSES:
                    run.status = "failed"
                    run.error = str(exc)[:1000]
                    run.finished_at = _now()
                    repository.update_run(run)
            else:
                logger.warning(
                    "daily brief job %s failure ignored after lease loss", job_id
                )
        else:
            finished = repository.finish_job(job_id, worker_id, lease_token)
            outcome = "done" if finished else "lease-lost"
            if not finished:
                logger.warning(
                    "daily brief job %s completion ignored after lease loss", job_id
                )
        return True
    finally:
        if outcome is None:
            # 异常逃逸(如 asyncio.wait 本身出错):确保任务回到可重试状态。
            # 必须用 requeue_job 而非 finish_job:finish_job 会把 job 置为
            # 终态 failed,claim_next_job 不再领取,错误消息声称的
            # "queued for retry" 不会发生(任务实际丢失);requeue_job 把
            # job 与 run 在同一事务里收敛回 queued/pending,与
            # claim_next_job 的过期恢复语义一致。
            # lease_token CAS 保证:若租约已被接管,这条写入自然失败。
            try:
                if repository.requeue_job(
                    job_id, worker_id, lease_token,
                    reason="worker unexpected exit; queued for retry",
                ):
                    logger.error(
                        "daily brief job %s requeued after unexpected exit", job_id
                    )
            except Exception:
                logger.exception(
                    "daily brief job %s failed to converge after unexpected exit", job_id
                )


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
    fairness = OwnerFairnessCursor()
    while not stop_event.is_set():
        claimed: tuple[DailyBriefRepository, dict[str, Any]] | None = None
        for repository in fairness.rotate(discover_repositories(data_root)):
            job = repository.claim_next_job(worker_id, lease_seconds)
            if job is not None:
                claimed = repository, job
                break
        if claimed is not None:
            try:
                keep_running = await run_claimed_job(
                    *claimed,
                    worker_id,
                    stop_event,
                    lease_seconds=lease_seconds,
                    service_factory=service_factory,
                )
            except Exception:
                # 单次任务异常不应终止整个 worker:run_claimed_job 的
                # finally 已尝试 requeue 收敛租约,这里只记录并继续下一轮
                # (否则 asyncio.run 崩溃会让进程退出,剩余任务全部无人处理)。
                logger.exception(
                    "daily brief worker continuing after unexpected job error"
                )
                # backoff（审核意见 P3）：DB 持续异常时避免紧密循环重扫
                # 全部 owner 库。
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=max(0.1, poll_seconds)
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            if not keep_running:
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

    # SDK source registry：与 Web 进程同源初始化（审核意见 P1）。否则本
    # 进程的 sdk_sources_available() 与 Web 手动分析不一致，evidence gate
    # 会错误地对测试类失败降级放行。
    try:
        from features.system import initialize_source_runtime

        state = initialize_source_runtime()
        logger.info("sdk source registry initialized: %s", state)
    except Exception:
        logger.exception("sdk source registry init failed; source gate stays strict")

    async def retry_source_init(stop_event: asyncio.Event) -> None:
        """初始化失败低频重试（审核意见 P2）：启动期密钥文件竞态/临时
        权限问题不应让本进程整个生命周期都保持 UNINITIALIZED（与 Web
        行为永久不一致，且强制取证会系统性烧光分析轮次）。"""
        from features.system import registry_state

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            if stop_event.is_set():
                return
            if registry_state() == "UNINITIALIZED":
                try:
                    from features.system import initialize_source_runtime

                    state = initialize_source_runtime()
                    logger.info("sdk source registry retry initialized: %s", state)
                except Exception:
                    logger.exception("sdk source registry retry init failed")

    async def run() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop_event.set)
        retry_task = asyncio.create_task(retry_source_init(stop_event))
        try:
            await worker_loop(
                stop_event,
                poll_seconds=max(0.1, args.poll_seconds),
                lease_seconds=max(10, args.lease_seconds),
            )
        finally:
            stop_event.set()
            retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await retry_task

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
