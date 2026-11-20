"""Shared state for manual reference syncs and scheduled radar scans."""
import fcntl
import os
import sys
import threading

from foundation.config import settings
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock


def initial_sync_status(**overrides):
    return {
        'running': False, 'mode': None, 'started_at': None, 'finished_at': None,
        'returncode': None, 'stdout': '', 'stderr': '', 'error': None, **overrides,
    }


class ReferenceScanner:
    def __init__(self, filename, **status):
        self.db_path = settings.data_root / filename
        self.lock = threading.Lock()
        self.status = initial_sync_status(**status)
        self.radar_descriptor = None


mainline_scanner = ReferenceScanner('mainline_known_issues.sqlite3')
update_scanner = ReferenceScanner('gms_update_monitor.sqlite3', source=[])


def run_reference_sync(args, scan):
    """Keep API children and direct CLI invocations under the same file lock."""
    if getattr(args, 'dry_run', False):
        return scan(args)
    path = args.db.with_suffix('.sync.lock')
    if args.sync_lock_fd is not None:
        # flock belongs to the open file description inherited from the Web process.
        if not os.path.samestat(os.fstat(args.sync_lock_fd), path.stat()):
            raise ValueError('sync file descriptor does not match the selected database')
        fcntl.flock(args.sync_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return scan(args)
    descriptor = try_acquire_sync_lock(path)
    if descriptor is None:
        print('error: sync already running', file=sys.stderr)
        return 1
    try:
        return scan(args)
    finally:
        release_sync_lock(descriptor)
