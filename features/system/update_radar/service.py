"""One durable scan at a time, with independent scan and mail outcomes."""

import asyncio
import logging
import threading
from functools import lru_cache

from features.email import send_email
from foundation.config import settings
from foundation.error_model import ApiError
from foundation.redaction import redact_sensitive_text

from . import scanning
from .models import utc_now
from .notifications import build_email
from .repository import RadarStore


logger = logging.getLogger(__name__)


class RadarService:
    def __init__(self, store, *, scanner=scanning.full_scan, mailer=send_email,
                 reserve=scanning.reserve_scanners, release=scanning.release_scanners):
        self.store, self.scanner, self.mailer = store, scanner, mailer
        self.reserve, self.release = reserve, release
        self.closing = threading.Event()
        self.worker = None
        self._mail_lock = threading.Lock()

    def start_scan(self, trigger='manual', *, owner_id='', username='', now=None):
        # 停机次序第一步：禁止领取新任务（manual 返回 409，schedule 由 watch 静默跳过）。
        if self.closing.is_set():
            raise ApiError.conflict('服务正在停止，本次扫描未启动')
        self.reserve()
        try:
            run_id = self.store.claim(trigger, owner_id=owner_id, username=username, now=now)
            if run_id is None:
                self.release()
                return None
        except Exception:
            self.release()
            raise
        self.worker = threading.Thread(target=self.execute, args=(run_id,), name='gms-update-radar', daemon=True)
        self.worker.start()
        return run_id

    def execute(self, run_id):
        error = None
        tasks = []
        try:
            tasks = self.store.run(run_id)['config']['tasks']
            summary, changes = self.scanner(
                cancelled=self.closing.is_set, tasks=tasks, cancel_event=self.closing)
            summary.setdefault('tasks', tasks)
            status = 'failed' if len(summary['errors']) >= summary.get('groups_total', 2) else 'partial' if summary['errors'] else 'completed'
            if self.closing.is_set():
                status = 'interrupted'
            self.store.finish(run_id, status, summary, changes)
            error = summary['errors'] or None
        except Exception as exc:
            logger.exception('Radar scan %s failed', run_id)
            error = redact_sensitive_text(getattr(exc, 'message', None) or str(exc) or '扫描执行失败')[:1500]
            summary = {'errors': {'radar': error}, 'changes_total': 0, 'categories': {}, 'tasks': tasks}
            self.store.finish(run_id, 'interrupted' if self.closing.is_set() else 'failed', summary, [])
        finally:
            self.release(error)
        if not self.closing.is_set():
            self.notify(run_id)

    def preview(self, run_id):
        run = self.store.run(run_id)
        return build_email(run, self.store.changes(run_id, limit=50)['items'])

    def notify(self, run_id, *, retry=False):
        with self._mail_lock:
            run = self.store.run(run_id)
            if run['status'] == 'running':
                raise ApiError.conflict('请等待本次扫描完成后发送汇总')
            if not retry and run['email_status'] != 'pending':
                return
            config = self.store.settings() if retry else run['config']
            summary = run['summary']
            if not retry and config['notify'] == 'changes' and not summary.get('changes_total') and not summary.get('errors'):
                self.store.mark_email(run_id, 'skipped')
                return
            if not config['recipients']:
                self.store.mark_email(run_id, 'unconfigured', '尚未设置收件人；请在雷达设置中填写后重发')
                return
            run['config'] = config
            try:
                subject, body = build_email(run, self.store.changes(run_id, limit=50)['items'])
            except Exception as exc:
                logger.exception('Radar email preview %s failed', run_id)
                self.store.mark_email(run_id, 'failed', redact_sensitive_text(
                    getattr(exc, 'message', None) or str(exc) or '邮件摘要生成失败'
                ))
                return
            self.store.mark_email(run_id, 'sending')
            try:
                result = self.mailer(config['recipients'], subject, body, channel='update_radar',
                                     owner_id=run['owner_id'], username=run['username'],
                                     ref_type='gms_update_radar_run', ref_id=str(run_id))
                self.store.mark_email(run_id, 'sent' if result.get('sent') else 'failed',
                                      redact_sensitive_text(result.get('error') or ''))
            except Exception as exc:
                logger.exception('Radar email %s failed', run_id)
                self.store.mark_email(run_id, 'failed', redact_sensitive_text(getattr(exc, 'message', None) or str(exc) or '邮件发送异常'))

    def tick(self, now=None):
        now = now or utc_now()
        config = self.store.settings()
        if config['enabled'] and config['next_run_at'] and config['next_run_at'] <= now.isoformat():
            return self.start_scan('schedule', now=now)
        return None

    async def watch(self):
        while not self.closing.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except ApiError as exc:
                logger.debug('Radar scheduling skipped: %s', getattr(exc, 'message', None) or exc)
            except Exception:
                logger.exception('Radar scheduling failed')
            await asyncio.sleep(30)


@lru_cache(maxsize=1)
def get_service():
    return RadarService(RadarStore(settings.data_root / 'gms_update_radar.sqlite3'))


def start_radar():
    service = get_service()
    service.closing.clear()
    service.store.recover()
    return asyncio.create_task(service.watch(), name='gms-update-radar-scheduler')


async def stop_radar(task):
    """Ordered shutdown: forbid new claims, cancel, kill scanners, join, recover.

    The scan worker forwards `closing` into each subprocess as cancel_event, so
    a scanner blocked in communicate() is killed within ~1s; we still join the
    worker with a bound before running store.recover() so the interrupted
    status is persisted after (not racing) the worker's own finish() write.
    """
    service = get_service()
    service.closing.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    worker = service.worker
    if worker is not None and worker.is_alive():
        # async 函数内不得同步 join（最长阻塞事件循环 30s）；放入线程等待。
        await asyncio.to_thread(worker.join, 30)
        if worker.is_alive():
            logger.warning('Radar scan worker did not finish within 30s during shutdown')
    service.store.recover()
