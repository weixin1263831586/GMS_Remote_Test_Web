"""Daily Brief CLI 入口（systemd timer / 手动运维用）。

用法::

    python -m features.redmine.daily_brief_cli run-nightly [--owner ID ...]
    python -m features.redmine.daily_brief_cli run-delta   [--owner ID ...]
    python -m features.redmine.daily_brief_cli doctor

- `--owner` 缺省时对**所有已启用** daily brief 的 owner 逐个执行
  （enabled 配置存于各 owner runtime 的 redmine_daily_brief 段）；
- 进程级互斥由 flock 保证（同机重复触发直接退出）；
- 单 owner 失败不影响其他 owner；exit code 汇总（0 全成功 / 1 有失败）。
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
from datetime import datetime


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_brief_cli")

LOCK_RELATIVE_PATH = "redmine/daily_brief_cli.lock"


def _owner_service(owner_id: str):
    """取 owner 的 DailyBriefService（config 走该 owner 的 runtime config）。"""
    from features.redmine.api import get_redmine_service_for_owner
    from features.redmine.daily_brief_service import DailyBriefService

    service = get_redmine_service_for_owner(owner_id)
    return DailyBriefService(owner_id, config_manager=service.agent.config_manager)


def _enabled_owner_ids(
    trigger_time: str | None = None, *, mode: str = "nightly"
) -> list[str]:
    """列出指定调度模式已启用且到点的 owner。"""
    from features.redmine.daily_brief_owner_policy import is_daily_brief_owner_eligible
    from foundation.config import settings

    root = settings.data_root / "redmine" / "by_user"
    owners: list[str] = []
    if not root.is_dir():
        return owners
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        owner_id = entry.name
        if not is_daily_brief_owner_eligible(owner_id):
            logger.info("skip administrator owner %s for daily brief", owner_id)
            continue
        try:
            service = _owner_service(owner_id)
            config = service.get_config()
            enabled_key = "delta_enabled" if mode == "delta" else "enabled"
            time_key = "delta_trigger_time" if mode == "delta" else "trigger_time"
            if (
                config.get("enabled")
                and config.get(enabled_key)
                and (trigger_time is None or config.get(time_key) == trigger_time)
            ):
                owners.append(owner_id)
        except Exception as exc:
            # 发现失败必须可见：静默跳过会让定时任务以「无 owner」正常退出。
            logger.warning("skip owner %s: config unreadable (%s: %s)",
                           owner_id, type(exc).__name__, exc, exc_info=True)
    return owners


def _run_mode(mode: str, owner_ids: list[str]) -> int:
    """enqueue-only：nightly/delta 定时触发只入队，不再直接执行 AI。

    审核意见 P1（统一执行域）：旧 CLI 在 systemd 进程里直接
    execute_run()，与 Durable Worker 形成两个并发执行域——nightly 与
    manual 可能同时各起一个 kkagent session，破坏
    max_parallel_issues=1 的全局语义，也无法统一取消/lease/重试。
    现在所有触发来源（Web manual / nightly / delta / reanalyze）统一：
    SQLite Queue → 唯一 Worker → kkagent。

    exit code 语义：入队成功=0；任何 owner 入队失败=1（执行结果由
    Worker 收敛进 runs 表，CLI 不等待）。
    """
    from features.redmine.daily_brief_owner_policy import is_daily_brief_owner_eligible

    # 两类定时入口每分钟调用一次，因此无 --owner 时只投递当前分钟配置的
    # owner。显式 --owner 是人工试跑，必须不受时刻筛选影响。
    due_time = datetime.now().strftime("%H:%M") if not owner_ids else None
    owners = owner_ids or _enabled_owner_ids(trigger_time=due_time, mode=mode)
    if not owners:
        logger.warning("no enabled daily-brief owners; nothing to do")
        return 0

    failures = 0
    for owner_id in owners:
        if not is_daily_brief_owner_eligible(owner_id):
            logger.info("skip administrator owner %s for daily brief", owner_id)
            continue
        try:
            service = _owner_service(owner_id)
            started = service.start_run(mode=mode)
            if started.get("reused"):
                logger.info("[%s] %s run already complete: %s", owner_id, mode, started["run_id"])
                continue
            if started.get("already_running"):
                logger.info("[%s] %s run in progress: %s", owner_id, mode, started["run_id"])
                continue
            if "run_id" not in started:
                logger.error("[%s] %s enqueue failed: %s", owner_id, mode,
                             started.get("error", "unknown"))
                failures += 1
                continue
            logger.info(
                "[%s] %s run queued: %s (job=%s)", owner_id, mode,
                started["run_id"], started.get("job_id", "-"),
            )
        except Exception:
            logger.exception("[%s] %s enqueue failed", owner_id, mode)
            failures += 1
    return 1 if failures else 0


def _doctor() -> int:
    """健康检查：kkagent 可用性、数据目录、enabled owner 数。"""
    import shutil

    from foundation.config import settings

    checks: list[tuple[str, bool, str]] = []
    kkagent = shutil.which("kkagent")
    checks.append(("kkagent binary", kkagent is not None, kkagent or "not on PATH"))
    data_root = settings.data_root
    checks.append(("data root", data_root.is_dir(), str(data_root)))
    owners = _enabled_owner_ids()
    checks.append(("enabled owners", bool(owners), f"{len(owners)}: {', '.join(owners) or '-'}"))

    exit_code = 0
    for name, ok, detail in checks:
        logger.info("%-16s %s (%s)", name, "OK" if ok else "FAIL", detail)
        exit_code |= 0 if ok else 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daily_brief_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run-nightly", "run-delta"):
        run_parser = sub.add_parser(name)
        run_parser.add_argument("--owner", action="append", default=[],
                                help="owner id (repeatable; default: all enabled)")
    sub.add_parser("doctor")
    args = parser.parse_args(argv)

    from foundation.config import settings

    lock_path = settings.data_root / LOCK_RELATIVE_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("another daily brief run holds %s; exiting", lock_path)
        return 0

    if args.command == "run-nightly":
        return _run_mode("nightly", args.owner)
    if args.command == "run-delta":
        return _run_mode("delta", args.owner)
    return _doctor()


if __name__ == "__main__":
    sys.exit(main())
