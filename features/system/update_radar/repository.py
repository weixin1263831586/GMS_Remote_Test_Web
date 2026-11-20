"""Durable schedule, scan claims, and before/after change records."""

import json
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from foundation.error_model import ApiError
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock
from foundation.sqlite_schema import ensure_schema

from .models import RadarConfig, next_scan, utc_now


# 单次运行最多持久化的变更明细：超出部分仅在 summary 里保留计数，防止异常
# 扫描（如全量翻页把每个条目都判定为变更）把 radar_changes 表撑爆。邮件与
# 页面详情每页读取量远小于该阈值，截断不影响使用。
MAX_CHANGES_PER_RUN = 2000
# 已完成运行的保留数量：更早的运行连同其变更明细一起清理，审计所需的
# 摘要、错误与邮件台账状态都在 radar_runs 主记录里。
RETAINED_RUNS = 200

SCHEMA = '''
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS radar_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL,
        next_run_at TEXT, owner_id TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS radar_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL,
        slot TEXT UNIQUE, status TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, config TEXT NOT NULL, owner_id TEXT NOT NULL,
        username TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '{}',
        email_status TEXT NOT NULL DEFAULT 'pending',
        email_error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS radar_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
        category TEXT NOT NULL, entity_key TEXT NOT NULL,
        change_type TEXT NOT NULL, before_json TEXT, after_json TEXT
    );
    CREATE INDEX IF NOT EXISTS radar_changes_run ON radar_changes(run_id,id);
'''


class RadarStore:
    def __init__(self, path: Path):
        self.path = path
        self.scan_lock = path.with_suffix('.scan.lock')
        self.mail_lock = path.with_suffix('.mail.lock')

    def _initialize(self, conn):
        with conn:
            conn.executescript(SCHEMA)
            conn.execute('INSERT OR IGNORE INTO radar_settings(id,config) VALUES(1,?)',
                         (json.dumps(RadarConfig().model_dump(mode='json')),))

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            ensure_schema(conn, self.path, self._initialize)
            yield conn
        finally:
            conn.close()

    def _initialize_for_reader(self, _reader):
        with closing(sqlite3.connect(self.path, timeout=30)) as writer:
            self._initialize(writer)

    @contextmanager
    def connect_readonly(self):
        if not self.path.exists():
            with self.connect():
                pass
        uri = self.path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as reader:
            reader.row_factory = sqlite3.Row
            ensure_schema(reader, self.path, self._initialize_for_reader)
            yield reader

    def settings(self):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM radar_settings WHERE id=1').fetchone()
            return {**RadarConfig.model_validate_json(row['config']).model_dump(mode='json'),
                    'next_run_at': row['next_run_at']}

    def save_settings(self, config, owner_id='', username='', now=None):
        now = now or utc_now()
        with self.connect() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM radar_settings WHERE id=1').fetchone()
            old = RadarConfig.model_validate_json(row['config']).model_dump(mode='json')
            changed = any(old[k] != config[k] for k in ('enabled', 'scan_time', 'timezone'))
            due = next_scan(config, now) if changed else row['next_run_at']
            conn.execute('UPDATE radar_settings SET config=?,next_run_at=?,owner_id=?,username=? WHERE id=1',
                         (json.dumps(config), due, owner_id, username))
        return self.settings()

    def claim(self, trigger, *, owner_id='', username='', now=None):
        now = now or utc_now()
        with self.connect() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute("SELECT 1 FROM radar_runs WHERE status='running'").fetchone():
                raise ApiError.conflict('雷达扫描正在进行中')
            row = conn.execute('SELECT * FROM radar_settings WHERE id=1').fetchone()
            config = RadarConfig.model_validate_json(row['config']).model_dump(mode='json')
            slot = None
            if trigger == 'schedule':
                due = row['next_run_at']
                if not config['enabled'] or not due or due > now.isoformat():
                    return None
                slot = due
                owner_id, username = row['owner_id'], row['username']
                conn.execute('UPDATE radar_settings SET next_run_at=? WHERE id=1',
                             (next_scan(config, now),))
            cursor = conn.execute('''INSERT INTO radar_runs
                (trigger,slot,status,started_at,config,owner_id,username)
                VALUES(?,?,'running',?,?,?,?)''',
                (trigger, slot, now.isoformat(), json.dumps(config), owner_id, username))
            return int(cursor.lastrowid)

    def finish(self, run_id, status, summary, changes):
        with self.connect() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            # Shutdown recovery and late scan-worker callbacks compete for this
            # row. Terminal states are immutable: a stale worker must not
            # resurrect an interrupted scan or duplicate its evidence rows.
            row = conn.execute('SELECT status FROM radar_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise ApiError.not_found('扫描记录不存在')
            if row['status'] != 'running':
                return
            if len(changes) > MAX_CHANGES_PER_RUN:
                # 真实总量保留在 summary（邮件与统计以此为准），明细只落前 N 条。
                summary = {**summary, 'changes_truncated': len(changes) - MAX_CHANGES_PER_RUN}
                changes = changes[:MAX_CHANGES_PER_RUN]
            conn.executemany('''INSERT INTO radar_changes
                (run_id,category,entity_key,change_type,before_json,after_json) VALUES(?,?,?,?,?,?)''',
                [(run_id, item['category'], item['key'], item['type'],
                  json.dumps(item['before'], ensure_ascii=False), json.dumps(item['after'], ensure_ascii=False))
                 for item in changes])
            conn.execute('UPDATE radar_runs SET status=?,summary=?,finished_at=? WHERE id=?',
                         (status, json.dumps(summary, ensure_ascii=False), utc_now().isoformat(), run_id))
            # 数据保留：终态运行只留最近 RETAINED_RUNS 条，孤儿变更明细一并清理。
            conn.execute('''DELETE FROM radar_runs WHERE status!='running' AND id NOT IN (
                SELECT id FROM radar_runs WHERE status!='running' ORDER BY id DESC LIMIT ?)''',
                (RETAINED_RUNS,))
            conn.execute('DELETE FROM radar_changes WHERE run_id NOT IN (SELECT id FROM radar_runs)')

    def mark_email(self, run_id, status, error=''):
        with self.connect() as conn, conn:
            conn.execute('UPDATE radar_runs SET email_status=?,email_error=? WHERE id=?',
                         (status, error[:1500], run_id))

    @staticmethod
    def decode(row):
        result = dict(row)
        result['summary'] = json.loads(result['summary'])
        result['config'] = RadarConfig.model_validate_json(result['config']).model_dump(mode='json')
        return result

    def run(self, run_id):
        with self.connect_readonly() as conn:
            row = conn.execute('SELECT * FROM radar_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise ApiError.not_found('扫描记录不存在')
            return self.decode(row)

    def history(self, limit=30, offset=0):
        with self.connect_readonly() as conn:
            rows = conn.execute('SELECT * FROM radar_runs ORDER BY id DESC LIMIT ? OFFSET ?',
                                (limit, offset)).fetchall()
            total = conn.execute('SELECT COUNT(*) FROM radar_runs').fetchone()[0]
            items = [self.decode(row) for row in rows]
            for item in items:
                item.pop('config')
            return {'items': items, 'total': total}

    def changes(self, run_id, limit=100, offset=0):
        with self.connect_readonly() as conn:
            rows = conn.execute('SELECT * FROM radar_changes WHERE run_id=? ORDER BY id LIMIT ? OFFSET ?',
                                (run_id, limit, offset)).fetchall()
            total = conn.execute('SELECT COUNT(*) FROM radar_changes WHERE run_id=?', (run_id,)).fetchone()[0]
            return {'items': [{**dict(row), 'before_json': json.loads(row['before_json']),
                               'after_json': json.loads(row['after_json'])} for row in rows], 'total': total}

    def recover(self):
        # A second Web process can start or stop while another is scanning or sending mail.
        with ExitStack() as stack:
            for path in (self.scan_lock, self.mail_lock):
                descriptor = try_acquire_sync_lock(path)
                if descriptor is None:
                    return
                stack.callback(release_sync_lock, descriptor)
            conn = stack.enter_context(self.connect())
            stack.enter_context(conn)
            conn.execute("""UPDATE radar_runs SET status='interrupted',finished_at=?
                WHERE status='running'""", (utc_now().isoformat(),))
            conn.execute("""UPDATE radar_runs SET
                email_status=CASE WHEN email_status='sending' THEN 'unknown' ELSE 'failed' END,
                email_error='服务重启；请核对邮件台账后重发本次汇总'
                WHERE email_status IN ('pending','sending')""")
