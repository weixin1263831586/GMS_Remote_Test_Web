"""Small, durable result snapshots and owner-scoped history queries."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from .models import TERMINAL_STATUSES


RESULT_OUTCOMES = frozenset({'passed', 'failed', 'incomplete', 'unknown'})


def report_snapshot(payload: Any) -> dict[str, Any]:
    """Keep explicit report counts; execution completion never implies a pass."""
    if not isinstance(payload, dict):
        return {}
    for wrapper in ('response', 'data'):
        if isinstance(payload.get(wrapper), dict):
            return report_snapshot(payload[wrapper])
    summary = payload.get('summary')
    if not isinstance(summary, dict):
        return {}
    counts = {}
    for target, aliases in {
        'total': ('total',), 'passed': ('pass', 'passed'), 'failed': ('failed', 'fail'),
    }.items():
        value = next((summary[key] for key in aliases if key in summary), None)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            continue
        try:
            number = int(value)
        except ValueError:
            continue
        if number >= 0:
            counts[target] = number
    if not counts:
        return {}
    complete = (
        all(key in counts for key in ('total', 'passed', 'failed'))
        and counts['total'] > 0
        and counts['passed'] + counts['failed'] == counts['total']
    )
    outcome = 'failed' if counts.get('failed', 0) > 0 else 'incomplete'
    if complete and counts['failed'] == 0:
        outcome = 'passed'
    details = payload.get('details')
    details = details if isinstance(details, dict) else {}
    return {
        **counts, 'outcome': outcome, 'counts_complete': complete,
        **{key: str(details[key])[:256] for key in ('suite_version', 'android_version')
           if details.get(key)},
    }


def encode_run_cursor(run: dict[str, Any]) -> str:
    value = json.dumps([run['created_at'], run['id']], separators=(',', ':'))
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip('=')


def decode_run_cursor(cursor: str) -> tuple[str, str]:
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError('Invalid run cursor')
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError('Invalid run cursor')
        if not all(isinstance(item, str) and item for item in value):
            raise ValueError('Invalid run cursor')
        return value[0], value[1]
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError('Invalid run cursor') from exc


def history_predicate(
    *, status: str = '', created_by: str = '', query: str = '',
    profile_id: str = '', worker_id: str = '', outcome: str = '',
    reports_only: bool = False, cursor: str = '',
) -> tuple[str, list[Any]]:
    clauses, values = [], []
    if status == 'running':
        terminal = sorted(TERMINAL_STATUSES)
        clauses.append('status NOT IN (' + ','.join('?' for _ in terminal) + ')')
        values.extend(terminal)
    elif status == 'failed':
        failures = sorted(TERMINAL_STATUSES - {'completed', 'cancelled'})
        clauses.append('status IN (' + ','.join('?' for _ in failures) + ')')
        values.extend(failures)
    elif status == 'testing':
        clauses.append("status IN ('testing', 'test_running')")
    elif status:
        clauses.append('status = ?')
        values.append(status)
    for column, value in (('created_by', created_by), ('profile_id', profile_id), ('worker_id', worker_id)):
        if value:
            clauses.append(f'{column} = ?')
            values.append(value)
    if query:
        columns = (
            'id', 'profile_id', 'project', 'branch', 'worker_id', 'devices_json',
            'artifact_path', 'gerrit_change_id', 'gerrit_subject', 'retry_of_run_id',
        )
        clauses.append('(' + ' OR '.join(f'instr(lower({column}), lower(?)) > 0' for column in columns) + ')')
        values.extend([query] * len(columns))
    if outcome:
        if outcome not in RESULT_OUTCOMES:
            raise ValueError('Invalid test outcome')
        clauses.append("COALESCE(json_extract(CASE WHEN json_valid(test_summary_json) "
                       "THEN test_summary_json ELSE '{}' END, '$.outcome'), 'unknown') = ?")
        values.append(outcome)
    if reports_only:
        clauses.append("report_timestamp != ''")
    if cursor:
        created_at, run_id = decode_run_cursor(cursor)
        clauses.append('(created_at < ? OR (created_at = ? AND id < ?))')
        values.extend((created_at, created_at, run_id))
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), values


def decorate_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    try:
        summary = json.loads(run.pop('test_summary_json', '{}'))
    except (ValueError, TypeError):
        summary = {}
    run['test_summary'] = summary if isinstance(summary, dict) else {}
    return run


def run_configuration(run: dict[str, Any]) -> dict[str, str]:
    """Expose only display fields, never the full plan or its credentials."""
    try:
        plan = json.loads(run.get('test_plan_json') or '{}')
    except (ValueError, TypeError):
        return {}
    if not isinstance(plan, dict):
        return {}
    return {key: plan[key] for key in ('test_type', 'test_suite', 'test_module', 'retry_dir')
            if isinstance(plan.get(key), str)}
