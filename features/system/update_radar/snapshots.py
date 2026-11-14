"""Compare business fields, excluding scan bookkeeping and SQLite row IDs."""

import hashlib
import json
import sqlite3
from pathlib import Path

from .models import CATEGORIES


TABLES = {
    'gms_update_artifacts': ('artifacts', ('source_key', 'item_key')),
    'gms_update_packages': ('packages', ('source_key', 'item_key')),
    'gms_update_mainline_packages': ('mainline', ('source_key', 'item_key')),
    'gms_update_bulletin_patches': ('bulletins', ('source_key', 'item_key')),
    'gms_update_requirement_sections': ('requirements', ('source_key', 'section_key')),
    'gms_update_requirement_table_rows': ('requirements', ('source_key', 'row_key')),
    'gms_update_requirement_version_tags': ('requirements', ('source_key', 'tag_key')),
    'mainline_known_issues': ('exemptions', ('product_section', 'issue_type', 'test_module', 'test_case', 'exemption_id')),
}
VOLATILE = {'id', 'content_hash', 'first_seen_at', 'last_seen_at', 'last_scanned_at', 'last_changed_at'}


def snapshot(path: Path) -> dict:
    result = {category: {} for category in CATEGORIES}
    if not path.exists():
        return result
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, (category, keys) in TABLES.items():
            if table not in available:
                continue
            for row in conn.execute(f'SELECT * FROM {table}'):  # constant table registry
                key = table + ':' + hashlib.sha256(json.dumps([row[k] for k in keys]).encode()).hexdigest()
                record = dict(row)
                result[category][key] = {k: record[k] for k in record if k not in VOLATILE}
    return result


def compare(before, after, categories=None):
    changes = []
    counts = {}
    for category in CATEGORIES if categories is None else categories:
        old, new = before[category], after[category]
        counts[category] = {'before': len(old), 'after': len(new), 'added': 0, 'modified': 0, 'removed': 0}
        for key in sorted(old.keys() | new.keys()):
            if old.get(key) == new.get(key):
                continue
            kind = 'added' if key not in old else 'removed' if key not in new else 'modified'
            counts[category][kind] += 1
            changes.append({'category': category, 'key': key, 'type': kind,
                            'before': old.get(key), 'after': new.get(key)})
    return counts, changes


def backup(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
