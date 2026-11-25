"""The only production scan CLI."""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from foundation.process_sync import release_sync_lock, try_acquire_sync_lock

from .repository import DB_PATH
from .scanner import run_scan
from .sources import SOURCE_KEYS


def main():
    p=argparse.ArgumentParser(description='Scan the seven GMS update sources into one database')
    p.add_argument('--db',type=Path,default=DB_PATH)
    p.add_argument('--mode',choices=('full','incremental'),default='full')
    p.add_argument('--source',action='append',choices=SOURCE_KEYS)
    p.add_argument('--trigger',choices=('manual','schedule','radar','cli'),default='cli')
    p.add_argument('--timeout',type=float,default=25)
    p.add_argument('--browser',choices=('auto','firefox','chromium'),default='auto')
    p.add_argument('--mainline-month-depth',type=int,choices=range(121),default=0)
    p.add_argument('--audit',action='store_true',help='Reconcile in a temporary database without modifying business data')
    p.add_argument('--sync-lock-fd',type=int,help=argparse.SUPPRESS)
    args=p.parse_args()
    path=args.db.with_suffix('.sync.lock')
    fd=args.sync_lock_fd
    owned=fd is None
    if owned:
        fd=try_acquire_sync_lock(path)
        if fd is None:
            print('scan already running',file=sys.stderr)
            return 2
    else:
        import fcntl
        if not os.path.samestat(os.fstat(fd),path.stat()):
            raise ValueError('scan lock does not match database')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        def execute(db):
            return run_scan(db,args.mode,args.source,trigger=args.trigger,
                            timeout=args.timeout,browser=args.browser,mainline_depth=args.mainline_month_depth)
        if args.audit:
            with TemporaryDirectory(prefix='gms-update-audit-') as directory:
                target=Path(directory)/'audit.sqlite3'
                if args.db.exists():
                    with closing(sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as dst:
                        src.backup(dst)
                result=execute(target)
        else:
            result=execute(args.db)
        print('GMS_SCAN='+json.dumps({**{k:v for k,v in result.items() if k!='errors'},'audit':args.audit}),flush=True)
        return int(result['state']!='completed')
    finally:
        if owned:
            release_sync_lock(fd)


if __name__=='__main__':
    raise SystemExit(main())
