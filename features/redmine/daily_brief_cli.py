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
import asyncio
import fcntl
import logging
import os
import sys


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_brief_cli")

LOCK_RELATIVE_PATH = "redmine/daily_brief_cli.lock"


def _owner_service(owner_id: str):
    """取 owner 的 DailyBriefService（config 走该 owner 的 runtime config）。"""
    from features.redmine.api import get_redmine_service_for_owner
    from features.redmine.daily_brief_service import DailyBriefService

    service = get_redmine_service_for_owner(owner_id)
    return DailyBriefService(owner_id, config_manager=service.agent.config_manager)


def _enabled_owner_ids() -> list[str]:
    """列出配置了 enabled=true 的 owner（数据目录下 by_user/*）。"""
    from foundation.config import settings

    root = settings.data_root / "redmine" / "by_user"
    owners: list[str] = []
    if not root.is_dir():
        return owners
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        owner_id = entry.name
        try:
            service = _owner_service(owner_id)
            if service.get_config().get("enabled"):
                owners.append(owner_id)
        except Exception as exc:
            # 发现失败必须可见：静默跳过会让定时任务以「无 owner」正常退出。
            logger.warning("skip owner %s: config unreadable (%s: %s)",
                           owner_id, type(exc).__name__, exc, exc_info=True)
    return owners


def _run_mode(mode: str, owner_ids: list[str]) -> int:
    owners = owner_ids or _enabled_owner_ids()
    if not owners:
        logger.warning("no enabled daily-brief owners; nothing to do")
        return 0

    failures = 0
    for owner_id in owners:
        try:
            service = _owner_service(owner_id)
            started = service.start_run(mode=mode)
            if started.get("reused"):
                logger.info("[%s] %s run already complete: %s", owner_id, mode, started["run_id"])
                continue
            if started.get("already_running"):
                logger.info("[%s] %s run in progress: %s", owner_id, mode, started["run_id"])
                continue
            run = asyncio.run(service.execute_run(started["run_id"]))
            status = run.status if run is not None else "unknown"
            logger.info("[%s] %s run %s -> %s", owner_id, mode, started["run_id"], status)
            if status not in ("completed", "partial"):
                failures += 1
        except Exception:
            logger.exception("[%s] %s run failed", owner_id, mode)
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
