"""Radar scan adapter and projection of committed GMS change events."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .. import service
from ..sources import TASK_SOURCES, sources_for_tasks
from .models import CATEGORIES


TABLES = {
    'gms_update_artifacts': ('artifacts', ('source_key', 'item_key')),
    'gms_update_packages': ('packages', ('source_key', 'item_key')),
    'gms_update_mainline_packages': ('mainline', ('source_key', 'item_key')),
    'gms_update_bulletin_patches': ('bulletins', ('source_key', 'item_key')),
    'gms_update_requirement_sections': ('requirements', ('source_key', 'section_key')),
    'gms_update_requirement_table_rows': ('requirements', ('source_key', 'row_key')),
    'gms_update_requirement_version_tags': ('requirements', ('source_key', 'tag_key')),
    'mainline_known_issues': ('exemptions', ('product_section', 'issue_type', 'test_module', 'test_case', 'exemption_id', 'android_versions')),
    'mainline_security_patches': ('security_patches', ('source_url', 'cve_id', 'mainline_module')),
}
VOLATILE = {'id', 'content_hash', 'first_seen_at', 'last_seen_at', 'last_scanned_at', 'last_changed_at',
            'detail_fetched_at', 'detail_status', 'table_index', 'row_index'}



def inventory_counts(path: Path) -> dict[str, int]:
    """Read only per-category inventory totals, never duplicate change detection."""
    result = dict.fromkeys(CATEGORIES, 0)
    if not path.exists():
        return result
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        # One consistent read snapshot across all business tables.
        conn.execute('BEGIN')
        available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, (category, _) in TABLES.items():
            if table in available:
                result[category] += conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    return result


def batch_changes(path: Path, run_id: int, before, after, categories):
    """Project scanner-owned events into radar's historical key and payload format.

    Inventory totals supply counts only. The committed scan batch is the
    authority for additions, modifications and removals, including partial runs.
    Keep the historical entity keys so period rollups can join earlier runs.
    """
    entity_tables = {
        'artifact': 'gms_update_artifacts',
        'gms_package': 'gms_update_packages',
        'mainline_package': 'gms_update_mainline_packages',
        'gms_bulletin': 'gms_update_bulletin_patches',
        'requirement_section': 'gms_update_requirement_sections',
        'requirement_table_row': 'gms_update_requirement_table_rows',
        'requirement_version_tag': 'gms_update_requirement_version_tags',
        'mainline_exemption': 'mainline_known_issues',
        'mainline_cve': 'mainline_security_patches',
    }
    counts = {
        category: {'before': before[category], 'after': after[category],
                   'added': 0, 'modified': 0, 'removed': 0}
        for category in categories
    }
    changes = []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT entity_type, change_type, before_json, after_json '
            'FROM gms_update_change_events WHERE run_id=? ORDER BY id', (run_id,))
        for row in rows:
            table = entity_tables[row['entity_type']]
            category, keys = TABLES[table]
            if category not in counts:
                continue
            old, new = (json.loads(row[field]) if row[field] else None
                        for field in ('before_json', 'after_json'))
            identity = new if new is not None else old
            key = table + ':' + hashlib.sha256(
                json.dumps([identity[k] for k in keys]).encode()).hexdigest()
            kind = {'added': 'added', 'changed': 'modified', 'removed': 'removed'}[row['change_type']]
            # Keep historical radar payloads stable except detail_status: the
            # scanner records that field as business evidence, so retain it.
            def public(record):
                return ({k: v for k, v in record.items()
                         if k not in VOLATILE or k == 'detail_status'} if record is not None else None)

            counts[category][kind] += 1
            changes.append({'category': category, 'key': key, 'type': kind,
                            'before': public(old), 'after': public(new)})
    return counts, sorted(changes, key=lambda item: (categories.index(item['category']), item['key']))



def reserve_scanners():
    service.reserve()


def release_scanners(error=None):
    service.release()


def full_scan(cancelled=lambda: False, tasks=None, cancel_event=None):
    tasks=list(CATEGORIES) if tasks is None else tasks
    sources=sources_for_tasks(tasks)
    before=inventory_counts(service.DB_PATH)
    if cancelled():
        raise InterruptedError('服务停止，扫描未启动')
    run=service.execute_reserved('full',sources,trigger='radar',cancel_event=cancel_event)
    after=inventory_counts(service.DB_PATH)
    errors={row['source_key']:row['error'] or row['state'] for row in run['sources']
            if row['state'] not in ('ready','unchanged')}
    if run['state'] not in ('completed','partial') and not errors:
        errors['radar']=run['error'] or '扫描未完整完成'
    category_errors={task:[key for key in TASK_SOURCES[task] if key in errors] for task in tasks}
    category_errors={k:v for k,v in category_errors.items() if v}
    counts,changes=batch_changes(service.DB_PATH,run['id'],before,after,tasks)
    origins={row['source_key']:row['origin'] for row in run['sources']}
    return {'categories':counts,'errors':errors,'category_errors':category_errors,
            'changes_total':len(changes),'tasks':tasks,'groups_total':len(sources),
            'scan_run_id':run['id'],'scan_state':run['state'],'origins':origins,
            'initial_baseline':not any(before[key] for key in tasks) and len(errors)<len(sources)},changes
