"""Run existing scanners in isolation and publish only complete successful scans."""

import os
import re
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from foundation.config import settings
from foundation.error_model import ApiError
from foundation.processes import run_local_command
from foundation.redaction import redact_sensitive_text

from ..mainline_issues import api as mainline_api
from ..update_monitor import api_support as update_api
from .models import utc_now
from .snapshots import backup, compare, snapshot


SCANNERS = (update_api, mainline_api)
TASK_SOURCES = {
    'requirements': ['gms_requirements'], 'packages': ['gms_downloads'],
    'mainline': ['mainline_preload'], 'artifacts': ['cts_downloads', 'vts_downloads', 'gts_downloads', 'gms_downloads'],
    'bulletins': ['gms_bulletins'],
}


def reserve_scanners():
    # Both manual API entrypoints check these flags under the same locks.
    with ExitStack() as stack:
        for api in SCANNERS:
            stack.enter_context(api._sync_lock)
        if any(api._sync_status['running'] for api in SCANNERS):
            raise ApiError.conflict('卡片扫描正在运行，请完成后再启动雷达')
        for api in SCANNERS:
            api._sync_status.update(running=True, mode='full', started_at=utc_now().isoformat(),
                                    finished_at=None, returncode=None, stdout='', stderr='', error=None)


def release_scanners(error=None):
    for group, api in zip(('updates', 'exemptions'), SCANNERS):
        detail = error.get(group) if isinstance(error, dict) else error
        with api._sync_lock:
            api._sync_status.update(running=False, finished_at=utc_now().isoformat(),
                                    returncode=1 if detail else 0, error=detail)


def scan_group(directory, group, api, module, flags, cancelled, cancel_event=None):
    if cancelled():
        raise InterruptedError('服务停止，本组未完成扫描，保留原数据')
    staged = Path(directory) / api.DB_PATH.name
    backup(api.DB_PATH, staged)
    result = run_local_command(
        [sys.executable, '-m', module, '--db', str(staged), *flags], timeout=3600,
        cwd=str(settings.project_root), env={**os.environ, 'GMS_PARTNER_ANDROID_AUTO_LOGIN': '0'},
        cancel_event=cancel_event,
    )
    if not result.ok:
        raise RuntimeError(redact_sensitive_text(result.stderr[-1500:] or '扫描进程失败、取消或超时'))
    if not staged.exists():
        raise RuntimeError('扫描未生成数据，本组保留原值')
    if group == 'exemptions':
        counters = dict(re.findall(r'(total_pages|pages_scanned|pages_skipped)=(\d+)', result.stdout))
        if (not counters or int(counters.get('total_pages', 0)) == 0
                or counters.get('total_pages') != counters.get('pages_scanned')):
            raise RuntimeError('Mainline 页面未完整扫描，本组数据保留原值；请检查 Partner 登录状态和扫描日志')
    else:
        with sqlite3.connect(staged) as conn:
            run = conn.execute('SELECT success,sources_scanned FROM gms_update_scan_runs ORDER BY id DESC LIMIT 1').fetchone()
            statuses = dict(conn.execute('SELECT source_key,status_code FROM gms_update_sources'))
        if not run or not run[0] or run[1] != flags.count('--source'):
            raise RuntimeError('GMS 更新数据源未完整扫描，本组数据保留原值')
        selected = [flags[index + 1] for index, item in enumerate(flags) if item == '--source']
        invalid = [source for source in selected if not 200 <= statuses.get(source, 0) < 300]
        if invalid:
            raise RuntimeError('数据源 HTTP 状态异常，保留原数据：' + '、'.join(invalid))
    current = snapshot(staged)
    if cancelled():
        raise InterruptedError('服务停止，本组未完成扫描，保留原数据')
    backup(staged, api.DB_PATH)
    return current


def full_scan(cancelled=lambda: False, tasks=None, cancel_event=None):
    """Run scan groups with staged databases and publish only complete results.

    ``cancel_event`` is forwarded to each scan subprocess (killed within ~1s
    of shutdown); ``cancelled()`` is checked between groups and around the
    publish step so a stop never swaps in half-scanned data.
    """
    from .models import CATEGORIES

    tasks = list(CATEGORIES) if tasks is None else tasks
    if not tasks or set(tasks) - CATEGORIES.keys():
        raise ValueError('请至少选择一个有效的监听任务')
    errors = {}
    before = snapshot(update_api.DB_PATH) if any(key != 'exemptions' for key in tasks) else {key: {} for key in CATEGORIES}
    if 'exemptions' in tasks:
        before['exemptions'] = snapshot(mainline_api.DB_PATH)['exemptions']
    after = {category: dict(records) for category, records in before.items()}
    sources = sorted({source for task in tasks for source in TASK_SOURCES.get(task, [])})
    groups = []
    if sources:
        flags = ['--mode', 'full']
        for source in sources:
            flags.extend(['--source', source])
        groups.append(('updates', update_api, 'features.system.update_monitor.cli', flags))
    if 'exemptions' in tasks:
        groups.append(('exemptions', mainline_api, 'features.system.mainline_issues.cli', ['--force']))
    settings.data_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='update-radar-', dir=settings.data_root) as directory:
        for group, api, module, flags in groups:
            try:
                current = scan_group(directory, group, api, module, flags, cancelled, cancel_event)
                for category in after:
                    if (category == 'exemptions') == (group == 'exemptions'):
                        after[category] = current[category]
            except Exception as exc:
                errors[group] = redact_sensitive_text(exc)[:1500]
    counts, changes = compare(before, after, tasks)
    return {'categories': counts, 'errors': errors, 'changes_total': len(changes),
            'tasks': tasks, 'groups_total': len(groups),
            'initial_baseline': not any(before[key] for key in tasks) and len(errors) < len(groups)}, changes
