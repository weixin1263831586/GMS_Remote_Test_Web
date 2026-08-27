"""校验安全审计 HMAC 链；显式授权后重建并保留原始证据。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PROJECT_ROOT / 'data/security_audit.json'
KEY_PATH = Path(
    os.getenv(
        'GMS_AUDIT_HMAC_KEY_FILE',
        str(PROJECT_ROOT / 'data/secrets/audit_hmac.key'),
    )
)


def load_key() -> bytes:
    if not KEY_PATH.exists():
        sys.exit(f'audit HMAC key not found: {KEY_PATH}')
    mode = stat.S_IMODE(KEY_PATH.stat().st_mode)
    if mode & 0o077:
        sys.exit(f'audit HMAC key permissions must be 0600: {KEY_PATH} (got {oct(mode)})')
    key = KEY_PATH.read_bytes().strip()
    if len(key) < 32:
        sys.exit('audit HMAC key must contain at least 32 bytes')
    return key


def canonical(record: dict) -> bytes:
    unsigned = {k: v for k, v in record.items() if k != 'record_hash'}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def record_hash(key: bytes, record: dict) -> str:
    return hmac.new(key, canonical(record), hashlib.sha256).hexdigest()


def _append_recovery_record(
    key: bytes,
    out_lines: list[bytes],
    previous: str,
    *,
    backup_name: str,
    source_sha256: str,
    broken_links: int,
    invalid_hmacs: int,
    dropped_lines: int,
) -> str:
    recovery = {
        'id': str(uuid4()),
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'action_type': 'maintenance',
        'source': 'local',
        'operation': 'audit_chain_rebuild',
        'status_code': 200,
        'details': {
            'backup': backup_name,
            'source_sha256': source_sha256,
            'broken_links': broken_links,
            'invalid_hmacs': invalid_hmacs,
            'dropped_invalid_json_lines': dropped_lines,
        },
        'previous_hash': previous,
    }
    recovery['record_hash'] = record_hash(key, recovery)
    out_lines.append(
        json.dumps(recovery, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        + b'\n'
    )
    return recovery['record_hash']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--log-path',
        type=Path,
        default=LOG_PATH,
        help='audit JSONL path (defaults to the application audit log)',
    )
    parser.add_argument(
        '--rebuild',
        action='store_true',
        help='rebuild a broken chain after preserving the original file',
    )
    parser.add_argument(
        '--allow-invalid-hmac',
        action='store_true',
        help='with --rebuild, explicitly permit re-signing records whose HMAC is invalid',
    )
    parser.add_argument(
        '--drop-invalid-json',
        action='store_true',
        help='with --rebuild, explicitly permit dropping unparseable JSON lines',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='deprecated compatibility flag; verification-only is now the default',
    )
    args = parser.parse_args()
    log_path = args.log_path.resolve()

    if not log_path.exists():
        print(f'audit log not found: {log_path}', file=sys.stderr)
        return 2

    key = load_key()

    lock_path = log_path.with_name(log_path.name + '.lock')
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(lock_path, 0o600)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)

    try:
        source_bytes = log_path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        records: list[dict] = []
        invalid_json_lines: list[int] = []
        with log_path.open(encoding='utf-8') as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    invalid_json_lines.append(line_number)
                    continue
                if isinstance(record, dict):
                    records.append(record)
                else:
                    invalid_json_lines.append(line_number)
        print(
            f'parsed {len(records)} records; '
            f'invalid JSON lines: {len(invalid_json_lines)}'
        )

        # Report current chain health.
        previous = 'GENESIS'
        breaks = 0
        hmac_bad = 0
        for rec in records:
            if str(rec.get('previous_hash') or '') != previous:
                breaks += 1
            expected = str(rec.get('record_hash') or '')
            if not hmac.compare_digest(expected, record_hash(key, rec)):
                hmac_bad += 1
            previous = expected
        print(f'current chain: {breaks} broken link(s), {hmac_bad} record(s) failing HMAC')

        if breaks == 0 and hmac_bad == 0 and not invalid_json_lines:
            print('chain is already valid; nothing to do')
            return 0

        if not args.rebuild or args.dry_run:
            print(
                'verification failed; no files were changed. '
                'Use --rebuild only after preserving and reviewing the original evidence.',
                file=sys.stderr,
            )
            return 1

        if invalid_json_lines and not args.drop_invalid_json:
            print(
                'refusing rebuild: invalid JSON lines would be lost; '
                'review the backup requirement and pass --drop-invalid-json explicitly',
                file=sys.stderr,
            )
            return 2

        if hmac_bad and not args.allow_invalid_hmac:
            print(
                'refusing rebuild: one or more records fail HMAC verification; '
                'investigate possible tampering and pass --allow-invalid-hmac explicitly '
                'only for an authorized recovery',
                file=sys.stderr,
            )
            return 2

        # Re-chain onto a fresh GENESIS.
        previous = 'GENESIS'
        out_lines: list[bytes] = []
        for rec in records:
            rebuilt = {k: v for k, v in rec.items() if k not in ('record_hash', 'previous_hash')}
            rebuilt['previous_hash'] = previous
            new_hash = record_hash(key, rebuilt)
            rebuilt['record_hash'] = new_hash
            previous = new_hash
            out_lines.append(json.dumps(rebuilt, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')

        # Preserve the original file first (don't unlink the only copy).
        ts = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup = log_path.with_name(f'{log_path.name}.corrupt-{ts}')
        shutil.copy2(log_path, backup)
        os.chmod(backup, 0o600)
        print(f'backed up original to {backup}')

        previous = _append_recovery_record(
            key,
            out_lines,
            previous,
            backup_name=backup.name,
            source_sha256=source_sha256,
            broken_links=breaks,
            invalid_hmacs=hmac_bad,
            dropped_lines=len(invalid_json_lines),
        )

        # Write to a sibling temporary file and atomically replace the log.
        tmp = log_path.with_name(f'.{log_path.name}.tmp')
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            for line in out_lines:
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, log_path)
        print(f'rewrote {log_path} ({len(out_lines)} records including recovery event)')

        # Verify the rebuilt chain end-to-end.
        previous = 'GENESIS'
        ok = 0
        with log_path.open(encoding='utf-8') as handle:
            for line_number, line in enumerate(handle, start=1):
                rec = json.loads(line)
                if str(rec.get('previous_hash') or '') != previous:
                    sys.exit(f'verify failed at line {line_number}: previous hash mismatch')
                expected = str(rec.get('record_hash') or '')
                if not hmac.compare_digest(expected, record_hash(key, rec)):
                    sys.exit(f'verify failed at line {line_number}: record HMAC mismatch')
                previous = expected
                ok += 1
        print(f'verified: {ok} records form a single valid chain head={previous}')
        return 0
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


if __name__ == '__main__':
    raise SystemExit(main())
