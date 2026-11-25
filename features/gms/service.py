"""Shared execution boundary for Web requests, scheduled radar and CLI."""
import os
import sys
import threading
from contextlib import closing

from foundation.config import settings
from foundation.error_model import ApiError
from foundation.process_sync import release_sync_lock, sync_lock_held, try_acquire_sync_lock
from foundation.processes import run_local_command
from foundation.redaction import redact_sensitive_text

from .parsing import utc_now
from .repository import DB_PATH, connect
from .scanner import recover_interrupted
from .sources import select_sources


_guard = threading.Lock()
_descriptor = None
_pending = None
_worker = None
_failure = None


def reserve():
    global _descriptor, _failure
    with _guard:
        if _descriptor is not None:
            raise ApiError.conflict('GMS 更新扫描正在运行')
        fd = try_acquire_sync_lock(DB_PATH.with_suffix('.sync.lock'))
        if fd is None:
            raise ApiError.conflict('GMS 更新扫描正在运行')
        _descriptor = fd
        _failure = None


def release():
    global _descriptor, _pending
    with _guard:
        if _descriptor is not None:
            release_sync_lock(_descriptor)
            _descriptor = None
        _pending = None


def get_run(run_id=None):
    if not DB_PATH.exists():
        return None
    with closing(connect(DB_PATH)) as conn:
        if run_id is None:
            row=conn.execute('SELECT * FROM gms_update_scan_runs ORDER BY id DESC LIMIT 1').fetchone()
        else:
            row=conn.execute('SELECT * FROM gms_update_scan_runs WHERE id=?',(run_id,)).fetchone()
        if not row:
            return None
        result=dict(row)
        result['sources']=[dict(r) for r in conn.execute(
            'SELECT * FROM gms_update_source_results WHERE run_id=? ORDER BY source_key',(row['id'],))]
        return result


def status():
    run=get_run()
    busy=sync_lock_held(DB_PATH.with_suffix('.sync.lock'))
    with _guard:
        pending=dict(_pending) if _pending else None
        failure=dict(_failure) if _failure else None
    if busy and pending and (not run or run['id']<=pending['previous_id']):
        return {**pending,'running':True,'state':'pending','db_exists':DB_PATH.exists(),
                'returncode':None,'error':None,'sources':[]}
    if not busy and failure and (not run or run['id']<=failure['previous_id'] or run['state']=='completed'):
        return {**failure,'running':False,'state':'failed','returncode':1,
                'db_exists':DB_PATH.exists(),'sources':[]}
    if not run:
        return {'running':busy,'state':'pending' if busy else 'idle','db_exists':DB_PATH.exists(),
                'returncode':None,'error':None,'sources':[]}
    state=run['state']
    if not busy and state=='running':
        state='interrupted'
    return {'run_id':run['id'],'running':busy,'state':state,'mode':run['mode'],
            'source':run['source_filter'].split(','),'started_at':run['started_at'],
            'finished_at':run['finished_at'] if state!='running' else None,
            'returncode':None if busy else int(state!='completed'),
            'error':None if busy or state=='completed' else
                ('扫描中断' if state=='interrupted' else run['error'] or '扫描未完整完成'),
            'db_exists':True,'sources':run['sources'],'changes_total':run['changes_total']}


def execute_reserved(mode, keys, *, trigger='manual', cancel_event=None):
    global _pending
    sources=[s.key for s in select_sources(keys)]
    previous=get_run()
    previous_id=previous['id'] if previous else 0
    with _guard:
        if _descriptor is None:
            raise RuntimeError('scanner reservation required')
        fd=_descriptor
        _pending={'previous_id':previous_id,'mode':mode,'source':sources,'started_at':utc_now()}
    argv=[sys.executable,'-m','features.gms.cli','--db',str(DB_PATH),
          '--mode',mode,'--trigger',trigger,'--sync-lock-fd',str(fd)]
    for source in sources:
        argv.extend(['--source',source])
    command=run_local_command(argv,timeout=3600,cwd=str(settings.project_root),
        env={**os.environ,'GMS_PARTNER_ANDROID_AUTO_LOGIN':'0'},
        cancel_event=cancel_event,pass_fds=(fd,))
    run=get_run()
    if run and run['id']>previous_id and run['state']=='running':
        with closing(connect(DB_PATH)) as conn, conn:
            recover_interrupted(conn)
        run=get_run()
    if not run or run['id']<=previous_id:
        raise RuntimeError(redact_sensitive_text(command.stderr or '扫描进程未创建批次')[:1000])
    if not command.ok and run['state']=='completed':
        with closing(connect(DB_PATH)) as conn, conn:
            conn.execute("UPDATE gms_update_scan_runs SET state='failed',success=0,error=? WHERE id=?",
                         ('扫描进程异常退出',run['id']))
        raise RuntimeError('扫描进程异常退出')
    if command.ok and run['state']!='completed':
        raise RuntimeError('扫描进程返回状态与批次状态不一致')
    return run


def _manual_worker(mode,keys):
    global _failure
    try:
        execute_reserved(mode,keys)
    except Exception as exc:
        with _guard:
            _failure={**(_pending or {'previous_id':0,'mode':mode,'source':keys}),
                      'error':redact_sensitive_text(str(exc))[:1000],'finished_at':utc_now()}
    finally:
        release()


def start(mode, keys):
    global _worker, _pending
    sources=[s.key for s in select_sources(keys)]
    reserve()
    try:
        previous=get_run()
        with _guard:
            _pending={'previous_id':previous['id'] if previous else 0,'mode':mode,
                      'source':sources,'started_at':utc_now()}
        _worker=threading.Thread(target=_manual_worker,args=(mode,sources),
                                 name='gms-update-scan',daemon=True)
        _worker.start()
    except Exception:
        release()
        raise
    return status()


def documents(run_id, limit, offset):
    run=get_run(run_id)
    if run is None:
        return None
    with closing(connect(DB_PATH)) as conn:
        total=conn.execute('SELECT COUNT(*) FROM gms_update_documents WHERE run_id=?',(run_id,)).fetchone()[0]
        items=[dict(r) for r in conn.execute("""SELECT source_key,url,status_code,content_hash,state,attempts
            FROM gms_update_documents WHERE run_id=? ORDER BY source_key,url LIMIT ? OFFSET ?""",
            (run_id,limit,offset))]
    return {'run':run,'documents':items,'total':total,'limit':limit,'offset':offset}
