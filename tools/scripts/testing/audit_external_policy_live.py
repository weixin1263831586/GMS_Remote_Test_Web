"""Read-only replay of retained Redmine snapshots against freshly indexed public CDD."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from features.knowledge.external import web_sources
from features.knowledge.external.web_sources import WebKnowledgeProvider
from features.redmine import reference_recall


def audit(data_root, scratch):
    index = scratch / 'public.sqlite3'
    provider = WebKnowledgeProvider(index)
    for version in (15, 16, 17):
        url = f'https://source.android.com/docs/compatibility/{version}/android-{version}-cdd'
        source = provider.save(name=f'Android {version} CDD', url=url, kind='cdd',
                               android_version=version, license='CC-BY-4.0')
        provider.sync(source)
    counts = Counter()
    seen = set()
    root = data_root / 'redmine' / 'by_user'
    for db in sorted(root.glob('*/evidence.sqlite3')):
        if len(seen) >= 12:
            break
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as cn:
                rows = cn.execute(
                    "SELECT snapshot_id,issue_id FROM redmine_evidence_snapshots WHERE status='ready' ORDER BY rowid DESC LIMIT 12"
                ).fetchall()
            for snapshot, issue_id in rows:
                if issue_id in seen or len(seen) >= 12:
                    continue
                seen.add(issue_id)
                try:
                    with patch.object(web_sources, 'index_path', return_value=index), patch.object(
                        reference_recall, 'owner_redmine_root', return_value=db.parent
                    ):
                        result = reference_recall.collect_snapshot_references(
                            db.parent.name, snapshot, data_root=data_root, allow_partner=False
                        )
                    policy = result.get('sources', {}).get('web_documents') or {}
                    counts['matched' if policy.get('items') else 'no_candidate'] += 1
                    counts['candidates'] += len(policy.get('items') or [])
                    counts['complete_snapshot'] += int(result.get('coverage_complete') is True)
                    counts['traceable_candidates'] += sum(
                        bool(item.get('reference') and item.get('source_revision') and item.get('source_url'))
                        for item in policy.get('items') or []
                    )
                except Exception:
                    counts['replay_errors'] += 1
        except sqlite3.Error:
            counts['unreadable_databases'] += 1
    counts['distinct_issues'] = len(seen)
    # No private issue IDs, subjects, logs, paths, owner identities or model inputs are exported.
    print('EXTERNAL_POLICY_REPLAY=' + json.dumps(dict(counts), sort_keys=True))
    if not seen or counts['replay_errors'] or counts['traceable_candidates'] != counts['candidates']:
        raise SystemExit('Retained issue replay incomplete')
    if not counts['matched']:
        raise SystemExit('No policy candidate on retained sample; review recall')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    audit(args.data_root, args.scratch)


if __name__ == '__main__':
    main()
