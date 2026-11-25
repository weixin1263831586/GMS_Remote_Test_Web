"""Durable summaries of observed changes over closed local calendar periods."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import CATEGORIES


PERIODS = {'weekly': '周报', 'monthly': '月报'}


def closed_periods(config, now):
    local = now.astimezone(ZoneInfo(config['timezone']))
    today = local.date()
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    for kind, start, end in (
        ('weekly', monday - timedelta(days=7), monday),
        ('monthly', (first - timedelta(days=1)).replace(day=1), first),
    ):
        if today == end and local.strftime('%H:%M') < config['scan_time']:
            continue
        yield kind, start, end


def period_bounds(start, end, zone):
    return tuple(datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
                 for day in (start, end))


def aggregate_period(kind, start, end, runs, changes_by_run, config):
    """Coalesce repeated entity changes, exposing incomplete scan/evidence coverage."""
    zone = ZoneInfo(config['timezone'])
    expected_days = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days)}
    tasks = list(dict.fromkeys([*config['tasks'], *(key for run in runs
                    for key in run['summary'].get('tasks', run['config']['tasks']))]))
    tasks = [key for key in CATEGORIES if key in tasks]
    states = {key: {'days': set(), 'failed_days': set(), 'baseline_days': set(),
                    'before': None, 'after': None, 'events': 0, 'gaps': 0}
              for key in tasks}
    transitions = {}
    omitted = 0
    for run in runs:
        summary = run['summary']
        day = datetime.fromisoformat(run['finished_at']).astimezone(zone).date().isoformat()
        errors = summary.get('errors') or {}
        selected = set(summary.get('tasks', run['config']['tasks']))
        evidence = changes_by_run.get(run['id'], [])
        counts = summary.get('categories') or {}
        expected = sum(int(counts.get(key, {}).get(action, 0))
                       for key in selected for action in ('added', 'modified', 'removed'))
        omitted += max(int(summary.get('changes_truncated') or 0), max(0, expected - len(evidence)))
        successful = set()
        for category in tasks:
            if category not in selected:
                continue
            state = states[category]
            group = 'exemptions' if category == 'exemptions' else 'updates'
            entry = counts.get(category)
            if (run['status'] not in ('completed', 'partial') or not entry
                    or category in summary.get('category_errors', {}) or group in errors or category in errors or 'radar' in errors):
                state['failed_days'].add(day)
                continue
            state['days'].add(day)
            successful.add(category)
            if state['after'] is not None and state['after'] != entry['before']:
                state['gaps'] += 1
            if summary.get('initial_baseline') and entry['before'] == 0:
                state['baseline_days'].add(day)
                if state['before'] is None:
                    state['before'] = entry['after']
            elif state['before'] is None:
                state['before'] = entry['before']
            state['after'] = entry['after']
        for item in evidence:
            category = item['category']
            if category not in successful:
                continue
            if summary.get('initial_baseline') and counts[category]['before'] == 0:
                continue
            state = states[category]
            state['events'] += 1
            key = (category, item['entity_key'])
            before, after = item['before_json'], item['after_json']
            if key not in transitions:
                transitions[key] = [before, after]
            else:
                if transitions[key][1] != before:
                    state['gaps'] += 1
                transitions[key][1] = after

    categories = {}
    for key, state in states.items():
        categories[key] = {
            'before': state['before'], 'after': state['after'],
            'added': 0, 'modified': 0, 'removed': 0, 'events': state['events'],
            'days_scanned': len(state['days']), 'days_total': len(expected_days),
            'missing_days': sorted(expected_days - state['days']),
            'failed_days': sorted(state['failed_days']),
            'baseline_days': sorted(state['baseline_days']),
            'evidence_gaps': state['gaps'],
        }
    changes = []
    reverted = 0
    for (category, key), (before, after) in sorted(transitions.items()):
        if before == after:
            reverted += 1
            continue
        action = 'added' if before is None else 'removed' if after is None else 'modified'
        categories[category][action] += 1
        changes.append({'category': category, 'key': key, 'type': action,
                        'before': before, 'after': after})
    incomplete = bool(omitted or any(
        row['missing_days'] or row['failed_days'] or row['baseline_days'] or row['evidence_gaps']
        for row in categories.values()))
    return {
        'period': {'kind': kind, 'start': start.isoformat(), 'end': (end - timedelta(days=1)).isoformat(),
                   'timezone': config['timezone'], 'runs': len(runs), 'days': len(expected_days),
                   'complete': not incomplete, 'evidence_omitted': omitted,
                   'reverted_entities': reverted},
        'tasks': tasks, 'categories': categories, 'changes_total': len(changes),
        'events_total': sum(row['events'] for row in categories.values()),
        'errors': {'period': '周期内存在缺扫、失败、基线或证据缺口；仅汇总已观察到的变化。'} if incomplete else {},
    }, changes
