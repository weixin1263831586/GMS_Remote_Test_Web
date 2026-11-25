"""Aggregate-only validation of a disposable unified database."""
import argparse
import json
from collections import Counter
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.gms import api
from features.gms import mainline_queries as mainline
from features.gms import queries as monitor
from features.gms.repository import connect
from features.gms.sources import SOURCE_KEYS


TABLES={
    'changes':('gms_update_change_events','/api/gms-update-center/changes',('id',)),
    'requirements':('gms_update_requirement_version_tags','/api/gms-update-center/requirements',('source_key','tag_key')),
    'packages':('gms_update_packages','/api/gms-update-center/packages',('source_key','item_key')),
    'mainline':('gms_update_mainline_packages','/api/gms-update-center/mainline',('source_key','item_key')),
    'artifacts':('gms_update_artifacts','/api/gms-update-center/artifacts',('source_key','item_key')),
    'exemptions':('mainline_known_issues','/api/gms-update-center/exemptions',('id',)),
    'patch':('mainline_security_patches','/api/gms-update-center/security-patches',('id',)),
    'critical':('gms_update_bulletin_patches','/api/gms-update-center/critical-patches',('source_key','item_key')),
}


def emit(stage,**values):
    print('UNIFIED_AUDIT='+json.dumps({'stage':stage,**values},ensure_ascii=True),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--phase',choices=('first','repeat'),required=True)
    args=parser.parse_args()
    if not args.db.is_file():
        emit('missing_database')
        return 1
    errors=[]
    with closing(connect(args.db)) as conn:
        runs=list(conn.execute('SELECT * FROM gms_update_scan_runs ORDER BY id DESC LIMIT 2'))
        if not runs:
            emit('missing_run')
            return 1
        run=runs[0]
        sources=list(conn.execute('SELECT * FROM gms_update_source_results WHERE run_id=? ORDER BY source_key',(run['id'],)))
        emit('run',phase=args.phase,run_id=run['id'],state=run['state'],sources_scanned=run['sources_scanned'],
             source_count=len(sources),changes=run['changes_total'])
        if set(r['source_key'] for r in sources)!=set(SOURCE_KEYS) or run['state']!='completed':
            errors.append('source_coverage')
        for row in sources:
            docs=list(conn.execute('SELECT state,status_code,attempts FROM gms_update_documents WHERE run_id=? AND source_key=?',(run['id'],row['source_key'])))
            status_counts=Counter(str(d['status_code']) for d in docs)
            state_counts=Counter(d['state'] for d in docs)
            emit('source',source=row['source_key'],state=row['state'],discovered=row['discovered'],
                 fetched=row['fetched'],parsed=row['parsed'],documents=len(docs),http=dict(status_counts),
                 document_states=dict(state_counts),origin=row['origin'])
            if row['state'] not in ('ready','unchanged') or row['discovered']!=row['fetched'] or row['discovered']!=len(docs):
                errors.append('incomplete_'+row['source_key'])
        counts={name:conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for name,(table,_,_) in TABLES.items()}
        if args.phase=='repeat' and len(runs)==2:
            unchanged=list(conn.execute("""SELECT a.source_key FROM gms_update_source_results a
                JOIN gms_update_source_results b ON a.source_key=b.source_key
                WHERE a.run_id=? AND b.run_id=? AND a.fingerprint!='' AND a.fingerprint=b.fingerprint""",
                (run['id'],runs[1]['id'])))
            for row in unchanged:
                events=conn.execute('SELECT COUNT(*) FROM gms_update_change_events WHERE run_id=? AND source_key=?',
                                    (run['id'],row['source_key'])).fetchone()[0]
                emit('idempotence',source=row['source_key'],events=events)
                if events:
                    errors.append('non_idempotent_'+row['source_key'])
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            errors.append('sqlite_integrity')
    # Use actual query routers against only the temporary file.
    monitor.DB_PATH=mainline.DB_PATH=args.db
    app=FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        for name,(_,url,keys) in TABLES.items():
            seen=set()
            total=None
            offset=0
            try:
                while total is None or offset<total:
                    response=client.get(url,params={'limit':500,'offset':offset})
                    response.raise_for_status()
                    result=response.json()
                    n=result.get('meta',{}).get('total',result.get('total'))
                    items=result.get('data',{}).get('items',result.get('items',[]))
                    if not isinstance(n,int) or (total is not None and total!=n):
                        raise ValueError('invalid pagination')
                    total=n
                    if not items and offset<total:
                        raise ValueError('incomplete pagination')
                    seen.update(tuple(item[k] for k in keys) for item in items)
                    offset+=len(items)
                match=total==offset==len(seen)==counts[name]
                emit('api',table=name,db=counts[name],api=total,unique=len(seen),match=match)
                if not match:
                    errors.append('api_'+name)
            except Exception as exc:
                emit('api_error',table=name,error_type=type(exc).__name__)
                errors.append('api_'+name)
    emit('complete',phase=args.phase,failures=errors)
    return int(bool(errors))


if __name__=='__main__':
    raise SystemExit(main())
