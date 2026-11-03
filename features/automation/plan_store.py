"""Atomic plan dispatch and follow-up receipts in the automation database."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .models import TERMINAL_STATUSES, utc_now_iso


class PlanConflictError(ValueError):
    pass


def initialize_plan_schema(conn: sqlite3.Connection) -> None:
    """Caller holds the process-safe schema write lock."""
    conn.execute('''CREATE TABLE IF NOT EXISTS automation_test_plans (
        id TEXT PRIMARY KEY, created_by TEXT NOT NULL, username TEXT NOT NULL,
        name TEXT NOT NULL, enabled INTEGER NOT NULL, stop_on_failure INTEGER NOT NULL,
        schedule_json TEXT NOT NULL, template_ciphertext TEXT NOT NULL,
        step_count INTEGER NOT NULL, next_run_at TEXT NOT NULL DEFAULT '',
        last_run_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
        active_batch_id TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS automation_plan_batches (
        id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, created_by TEXT NOT NULL,
        username TEXT NOT NULL, template_ciphertext TEXT NOT NULL,
        stop_on_failure INTEGER NOT NULL, step_count INTEGER NOT NULL,
        next_step INTEGER NOT NULL, last_run_id TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '',
        had_failures INTEGER NOT NULL DEFAULT 0
    )''')
    if 'had_failures' not in {row['name'] for row in conn.execute('PRAGMA table_info(automation_plan_batches)')}:
        conn.execute('ALTER TABLE automation_plan_batches ADD COLUMN had_failures INTEGER NOT NULL DEFAULT 0')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_test_plans_due ON automation_test_plans(enabled, next_run_at)')
    if 'labels_json' not in {row['name'] for row in conn.execute('PRAGMA table_info(automation_test_plans)')}:
        conn.execute("ALTER TABLE automation_test_plans ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]'")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_plan_batches_active ON automation_plan_batches(status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_runs_followup ON automation_runs(followup_state, status)')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_pending_followup ON automation_runs(updated_at,id) "
                 "WHERE followup_state='' AND (retry_limit>0 OR plan_batch_id!='')")


def insert_prepared_run(conn: sqlite3.Connection, prepared: dict[str, Any]) -> None:
    from .repository import RUN_COLUMNS

    run = prepared['run']
    columns = ','.join(RUN_COLUMNS)
    marks = ','.join('?' for _ in RUN_COLUMNS)
    conn.execute(f'INSERT INTO automation_runs ({columns}) VALUES ({marks})',
                 [str(run.get(key, '')) for key in RUN_COLUMNS])
    for name, encrypted in prepared.get('secrets', {}).items():
        conn.execute('''INSERT INTO automation_run_secrets (run_id,name,encrypted_value,updated_at)
                        VALUES (?,?,?,?)''', (run['id'], name, encrypted, utc_now_iso()))
    conn.execute('''INSERT INTO automation_run_events
        (run_id,stage,level,message,payload_json,trace_id,event_type,operation_id,from_status,to_status,created_at)
        VALUES (?,?,'info','Automation run queued',?,?, 'run.created',?,'',?,?)''',
                 (run['id'], run['status'], json.dumps({'plan_id': run.get('plan_id', '')}),
                  run['id'], run['id'] + ':create', run['status'], run['created_at']))


class PlanStore:
    def __init__(self, store):
        self.store = store

    def get(self, plan_id: str) -> dict | None:
        with self.store._connect() as conn:
            row = conn.execute('SELECT * FROM automation_test_plans WHERE id=?', (plan_id,)).fetchone()
        return dict(row) if row else None

    def list(self, *, owner: str = '', label: str = '') -> list[dict]:
        where, params = [], []
        if owner:
            where.append('created_by=?')
            params.append(owner)
        if label:
            where.append('EXISTS (SELECT 1 FROM json_each(labels_json) WHERE value=?)')
            params.append(label)
        with self.store._connect() as conn:
            rows = conn.execute('SELECT * FROM automation_test_plans ' +
                                ('WHERE ' + ' AND '.join(where) + ' ' if where else '') +
                                'ORDER BY created_at DESC, id DESC LIMIT 200', params).fetchall()
        return [dict(row) for row in rows]

    def save(self, data: dict, *, version: int = 0) -> dict:
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM automation_test_plans WHERE id=?', (data['id'],)).fetchone()
            if old:
                if old['created_by'] != data['created_by'] or old['version'] != version:
                    raise PlanConflictError('Plan changed; reload before saving')
                fields = ('name', 'enabled', 'stop_on_failure', 'schedule_json', 'template_ciphertext',
                          'step_count', 'next_run_at', 'updated_at', 'labels_json')
                assignments = ','.join(key + '=?' for key in fields)
                conn.execute(f'UPDATE automation_test_plans SET {assignments},version=version+1,last_error=\'\' WHERE id=?',
                             [*(data[key] for key in fields), data['id']])
            else:
                if version:
                    raise PlanConflictError('Plan no longer exists')
                fields = tuple(data)
                conn.execute('INSERT INTO automation_test_plans (' + ','.join(fields) + ') VALUES (' +
                             ','.join('?' for _ in fields) + ')', [data[key] for key in fields])
        return self.get(data['id'])

    def set_enabled(self, plan_id: str, enabled: bool, next_run_at: str, *, version: int) -> dict:
        with self.store._connect() as conn:
            changed = conn.execute('''UPDATE automation_test_plans
                SET enabled=?,next_run_at=?,version=version+1,updated_at=?,last_error=''
                WHERE id=? AND version=?''', (enabled, next_run_at, utc_now_iso(), plan_id, version))
            if changed.rowcount != 1:
                raise PlanConflictError('Plan changed; reload before updating')
        return self.get(plan_id)

    def due(self, now: str) -> list[dict]:
        with self.store._connect() as conn:
            rows = conn.execute('''SELECT * FROM automation_test_plans
                WHERE enabled=1 AND next_run_at!='' AND next_run_at<=?
                ORDER BY next_run_at,id LIMIT 25''', (now,)).fetchall()
        return [dict(row) for row in rows]

    def start_batch(self, plan: dict, batch_id: str, first: dict | None, *, now: str,
                    next_run_at: str, due: bool) -> bool:
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM automation_test_plans WHERE id=?', (plan['id'],)).fetchone()
            if not current or current['version'] != plan['version']:
                raise PlanConflictError('Plan changed before dispatch')
            if due and (not current['enabled'] or not current['next_run_at'] or current['next_run_at'] > now):
                return False
            if current['active_batch_id']:
                if due:
                    conn.execute('UPDATE automation_test_plans SET next_run_at=?,last_error=? WHERE id=?',
                                 (next_run_at, 'Previous batch is still active; scheduled occurrence skipped', plan['id']))
                    return False
                raise PlanConflictError('This plan already has an active batch')
            if first is None:
                return False
            insert_prepared_run(conn, first)
            conn.execute('''INSERT INTO automation_plan_batches
                (id,plan_id,created_by,username,template_ciphertext,stop_on_failure,step_count,
                 next_step,last_run_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,'running',?)''',
                         (batch_id, plan['id'], plan['created_by'], plan['username'], plan['template_ciphertext'],
                          plan['stop_on_failure'], plan['step_count'], 1, first['run']['id'], now))
            conn.execute('''UPDATE automation_test_plans SET active_batch_id=?,last_run_at=?,last_error='',
                next_run_at=? WHERE id=?''', (batch_id, now, next_run_at if due else current['next_run_at'], plan['id']))
        return True

    def defer_error(self, plan: dict, *, next_run_at: str, message: str) -> None:
        with self.store._connect() as conn:
            conn.execute('''UPDATE automation_test_plans SET next_run_at=?,last_error=?
                WHERE id=? AND version=? AND next_run_at=?''',
                         (next_run_at, message, plan['id'], plan['version'], plan['next_run_at']))

    def batches(self, plan_id: str = '', *, active_only: bool = False) -> list[dict]:
        with self.store._connect() as conn:
            rows = conn.execute('SELECT * FROM automation_plan_batches WHERE ' +
                                ("status IN ('running','stopping')" if active_only else 'plan_id=?') +
                                ' ORDER BY created_at DESC LIMIT 200', [] if active_only else [plan_id]).fetchall()
        return [dict(row) for row in rows]

    def advance_batch(self, batch: dict, prepared: dict | None, *, status: str = 'completed', step_failed: bool = False) -> bool:
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM automation_plan_batches WHERE id=?', (batch['id'],)).fetchone()
            if not current or current['status'] not in ('running', 'stopping') or current['last_run_id'] != batch['last_run_id']:
                return False
            if prepared and current['status'] != 'running':
                return False
            previous = conn.execute('SELECT followup_state FROM automation_runs WHERE id=?', (current['last_run_id'],)).fetchone()
            if not previous or previous['followup_state'] in ('', 'retried'):
                return False
            if prepared:
                insert_prepared_run(conn, prepared)
                conn.execute('UPDATE automation_plan_batches SET next_step=next_step+1,last_run_id=?,had_failures=MAX(had_failures,?) WHERE id=?',
                             (prepared['run']['id'], int(step_failed), batch['id']))
            else:
                if status == 'completed' and (current['had_failures'] or step_failed):
                    status = 'failed'
                conn.execute('UPDATE automation_plan_batches SET status=?,finished_at=? WHERE id=?',
                             (status, utc_now_iso(), batch['id']))
                conn.execute("UPDATE automation_test_plans SET active_batch_id='',last_error=? WHERE id=? AND active_batch_id=?",
                             ('A test step failed, had unknown results, or could not be dispatched' if status == 'failed' else '',
                              batch['plan_id'], batch['id']))
        return True

    def stop_batch(self, plan_id: str) -> None:
        """Stop future steps; an already-running test is cancelled via its own API."""
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT active_batch_id FROM automation_test_plans WHERE id=?', (plan_id,)).fetchone()
            if row and row['active_batch_id']:
                conn.execute("UPDATE automation_plan_batches SET status='stopping' WHERE id=? AND status='running'",
                             (row['active_batch_id'],))
                pending = conn.execute('''SELECT id FROM automation_runs
                    WHERE plan_batch_id=? AND source_type='auto_retry' AND status='queued'
                      AND started_at='' AND lease_owner='' ''', (row['active_batch_id'],)).fetchall()
                for run in pending:
                    now = utc_now_iso()
                    conn.execute("UPDATE automation_runs SET status='cancelled',current_stage='cancelled',"
                                 "followup_state='done',finished_at=?,updated_at=?,state_version=state_version+1 WHERE id=?",
                                 (now, now, run['id']))
                    conn.execute('''INSERT INTO automation_run_events
                        (run_id,stage,level,message,payload_json,trace_id,event_type,operation_id,from_status,to_status,created_at)
                        VALUES (?,'cancelled','info','Pending automatic retry stopped with its plan batch','{}',?,
                                'run.cancelled',?,'queued','cancelled',?)''',
                                 (run['id'], run['id'], run['id'] + ':plan-stop', now))

    def terminal_followups(self) -> list[dict]:
        statuses = sorted(TERMINAL_STATUSES)
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM automation_runs WHERE followup_state='' "
                                "AND (retry_limit>0 OR plan_batch_id!='') AND status IN (" +
                                ','.join('?' for _ in statuses) + ') ORDER BY updated_at,id LIMIT 100', statuses).fetchall()
        return [dict(row) for row in rows]

    def finish_followup(self, run: dict, *, decision: str, prepared: dict | None = None) -> bool:
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute("SELECT * FROM automation_runs WHERE id=? AND followup_state=''", (run['id'],)).fetchone()
            if not current or current['status'] not in TERMINAL_STATUSES:
                return False
            if prepared and run.get('plan_batch_id'):
                batch = conn.execute('SELECT status FROM automation_plan_batches WHERE id=?', (run['plan_batch_id'],)).fetchone()
                if not batch or batch['status'] != 'running':
                    prepared, decision = None, 'batch_stopped'
            if prepared:
                insert_prepared_run(conn, prepared)
                conn.execute('''UPDATE automation_plan_batches SET last_run_id=?
                    WHERE id=? AND last_run_id=? AND status='running' ''',
                             (prepared['run']['id'], run.get('plan_batch_id', ''), run['id']))
            conn.execute('UPDATE automation_runs SET followup_state=? WHERE id=?', (decision, run['id']))
            if int(run.get('retry_limit') or 0):
                conn.execute('''INSERT INTO automation_run_events
                    (run_id,stage,level,message,payload_json,trace_id,event_type,operation_id,created_at)
                    VALUES (?,'retry','info',?,?,?,'run.retry_decided',?,?)''',
                             (run['id'], decision, json.dumps({'retry_run_id': prepared['run']['id'] if prepared else ''}),
                              run['id'], run['id'] + ':retry-decision', utc_now_iso()))
        return prepared is not None
