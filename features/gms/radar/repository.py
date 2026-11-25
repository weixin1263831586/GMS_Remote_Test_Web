"""Durable schedule, scan claims, and before/after change records."""

import json
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from foundation.database import ensure_schema
from foundation.error_model import ApiError
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock

from .models import RadarConfig, next_scan, utc_now
from .periods import aggregate_period, closed_periods, period_bounds


# 限制单次每日扫描的明细规模；超出部分保留总量与截断标记，周期汇总
# 将缺失明细计为证据不完整，而不是把截断后的结果当成完整变化。
MAX_CHANGES_PER_RUN = 2000
# 每日终态记录仅在超出保留天数且不属于最近保留数量时清理。
# 周期快照独立保留，不随每日历史删除。
RETAINED_RUNS = 200
RETAINED_DAYS = 62
MAX_PERIOD_CHANGES = 20000

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

    def finish(self, run_id, status, summary, changes, *, now=None):
        now = now or utc_now()
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
                         (status, json.dumps(summary, ensure_ascii=False), now.isoformat(), run_id))
            # 清理同时超出每日保留天数及保留数量的终态记录与孤儿明细。
            conn.execute('''DELETE FROM radar_runs WHERE trigger IN ('manual','schedule')
                AND status!='running' AND julianday(finished_at)<=julianday(?) AND id NOT IN (
                SELECT id FROM radar_runs WHERE trigger IN ('manual','schedule') AND status!='running'
                ORDER BY id DESC LIMIT ?)''',
                ((now - timedelta(days=RETAINED_DAYS)).isoformat(), RETAINED_RUNS))
            conn.execute('DELETE FROM radar_changes WHERE run_id NOT IN (SELECT id FROM radar_runs)')

    def prepare_periods(self, now=None):
        """Close each calendar period once; keep snapshots independently of daily history."""
        now = now or utc_now()
        created = []
        with self.connect() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            setting = conn.execute('SELECT * FROM radar_settings WHERE id=1').fetchone()
            config = RadarConfig.model_validate_json(setting['config']).model_dump(mode='json')
            if not config['enabled']:
                return created
            if conn.execute("SELECT 1 FROM radar_runs WHERE status='running'").fetchone():
                return created
            zone = ZoneInfo(config['timezone'])
            for kind, start, end in closed_periods(config, now):
                slot = f'{kind}:{start.isoformat()}'
                if conn.execute('SELECT 1 FROM radar_runs WHERE slot=?', (slot,)).fetchone():
                    continue
                begin, finish = period_bounds(start, end, zone)
                raw = conn.execute("""SELECT * FROM radar_runs
                    WHERE trigger IN ('manual','schedule') AND status!='running'
                      AND julianday(finished_at)>=julianday(?) AND julianday(finished_at)<julianday(?)
                    ORDER BY julianday(finished_at),id""", (begin.isoformat(), finish.isoformat())).fetchall()
                if not raw and not conn.execute("""SELECT 1 FROM radar_runs
                    WHERE trigger IN ('manual','schedule') AND julianday(finished_at)<julianday(?) LIMIT 1""",
                    (finish.isoformat(),)).fetchone():
                    continue
                runs = [self.decode(row) for row in raw]
                evidence = {}
                for run in runs:
                    evidence[run['id']] = [
                        {**dict(row), 'before_json': json.loads(row['before_json']),
                         'after_json': json.loads(row['after_json'])}
                        for row in conn.execute('SELECT * FROM radar_changes WHERE run_id=? ORDER BY id', (run['id'],))
                    ]
                summary, changes = aggregate_period(kind, start, end, runs, evidence, config)
                if len(changes) > MAX_PERIOD_CHANGES:
                    summary['changes_truncated'] = len(changes) - MAX_PERIOD_CHANGES
                    summary['period']['complete'] = False
                    summary['errors']['period'] = '周期证据明细达到存储上限；统计与覆盖率仍保留。'
                    changes = changes[:MAX_PERIOD_CHANGES]
                cursor = conn.execute("""INSERT INTO radar_runs
                    (trigger,slot,status,started_at,finished_at,config,owner_id,username,summary)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (kind, slot, 'completed' if summary['period']['complete'] else 'partial',
                     begin.isoformat(), now.isoformat(), json.dumps(config), setting['owner_id'],
                     setting['username'], json.dumps(summary, ensure_ascii=False)))
                run_id = int(cursor.lastrowid)
                conn.executemany("""INSERT INTO radar_changes
                    (run_id,category,entity_key,change_type,before_json,after_json) VALUES(?,?,?,?,?,?)""",
                    [(run_id, item['category'], item['key'], item['type'],
                      json.dumps(item['before'], ensure_ascii=False), json.dumps(item['after'], ensure_ascii=False))
                     for item in changes])
                created.append(run_id)
        return created

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

    def history(self, limit=30, offset=0, kind='all'):
        where = {
            'daily': " WHERE trigger IN ('manual','schedule')",
            'weekly': " WHERE trigger='weekly'",
            'monthly': " WHERE trigger='monthly'",
            'all': '',
        }[kind]
        with self.connect_readonly() as conn:
            rows = conn.execute('SELECT * FROM radar_runs' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?',
                                (limit, offset)).fetchall()
            total = conn.execute('SELECT COUNT(*) FROM radar_runs' + where).fetchone()[0]
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
