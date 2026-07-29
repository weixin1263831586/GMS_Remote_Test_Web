"""
Security audit logging for web and CLI operations.
"""
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


SENSITIVE_KEYWORDS = (
    'password',
    'passwd',
    'pswd',
    'token',
    'secret',
    'api_key',
    'apikey',
    'authorization',
    'cookie',
    'pair_code',
)

_WORD_BOUNDARY_PATTERN = re.compile(r'(?:' + '|'.join(re.escape(kw) for kw in SENSITIVE_KEYWORDS) + r')', re.IGNORECASE)


class SecurityAuditLogger:
    """Append-only, HMAC hash-chained JSONL audit log."""

    # 日志超过软上限后，在下次写入时裁剪并重建哈希链。
    MAX_LOG_BYTES = 50 * 1024 * 1024
    # When rotating, retain the most recent this-many bytes of the tail.
    ROTATE_KEEP_BYTES = 10 * 1024 * 1024

    def __init__(self, log_path: str | None = None, max_read_lines: int = 5000):
        # 审计日志存放在项目运行数据目录。
        # data_root (e.g. data/security_audit.json), not next to the feature
        # module. The previous base_dir math landed in features/data/ because
        # this file lives at features/system/, so two dirnames up only reaches
        # features/.
        if log_path is None:
            from foundation.config import settings

            # Pytest deliberately skips deployment runtime settings. Keep its
            # audit traffic in a separate file so test requests can never
            # append records signed with a test key to the production chain.
            filename = (
                'security_audit.test.json'
                if os.getenv('GMS_SKIP_RUNTIME_ENV')
                else 'security_audit.json'
            )
            log_path = str(settings.data_root / filename)
        self.log_path = log_path
        self.lock_path = f'{log_path}.lock'
        self.max_read_lines = max_read_lines
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        if os.path.exists(self.log_path):
            os.chmod(self.log_path, 0o600)
        # Parsed-record cache keyed on the log file's mtime. The audit log is
        # append-only, so its mtime changes exactly when a record is added —
        # reading it inside the same lock as writes gives a cheap invalidation
        # signal. Without this, every page load re-read the whole file and
        # json.loads'd up to 5000 lines (the security-audit page was slow).
        self._cache_mtime: float | None = None
        self._cache_records: list[dict[str, Any]] = []
        self._cache_stats: dict[str, int] = {'total': 0, 'web': 0, 'cli': 0, 'api': 0, 'page_view': 0, 'errors': 0}
        self._head_hash: str | None = None

    @contextmanager
    def _cross_process_lock(self, exclusive: bool = True):
        """Serialize audit reads/writes across uvicorn workers and processes."""
        parent = os.path.dirname(self.lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _audit_key(self) -> bytes:
        from foundation.config import settings

        injected = os.getenv('GMS_AUDIT_HMAC_KEY', '').strip()
        if injected:
            key = injected.encode('utf-8')
        else:
            configured = os.getenv('GMS_AUDIT_HMAC_KEY_FILE', '').strip()
            path = Path(configured) if configured else settings.data_root / 'secrets/audit_hmac.key'
            if path.exists():
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise RuntimeError(f'audit HMAC key permissions must be 0600: {path}')
                key = path.read_bytes().strip()
            else:
                production = os.getenv('GMS_ENV', settings.environment).strip().lower() == 'production'
                if production:
                    raise RuntimeError('GMS_AUDIT_HMAC_KEY or a mode-0600 key file is required')
                path.parent.mkdir(parents=True, exist_ok=True)
                key = os.urandom(32).hex().encode('ascii')
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(descriptor, key + b'\n')
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        if len(key) < 32:
            raise RuntimeError('audit HMAC key must contain at least 32 bytes')
        return key

    def validate_configuration(self) -> None:
        """Load the signing key and reject an already-corrupt audit chain."""

        self._audit_key()
        result = self.verify_chain()
        if not result.get('valid'):
            raise RuntimeError(
                f"security audit chain verification failed at line "
                f"{result.get('line', '?')}: {result.get('error', 'unknown error')}"
            )

    @staticmethod
    def _canonical_record(record: dict[str, Any]) -> bytes:
        unsigned = {key: value for key, value in record.items() if key != 'record_hash'}
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')

    def _record_hash(self, record: dict[str, Any]) -> str:
        return hmac.new(
            self._audit_key(),
            self._canonical_record(record),
            hashlib.sha256,
        ).hexdigest()

    def _last_hash_locked(self) -> str:
        if self._head_hash is not None:
            return self._head_hash
        if not os.path.exists(self.log_path):
            return 'GENESIS'
        for raw in reversed(self._tail_lines(1)):
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            value = str(record.get('record_hash') or '')
            if value:
                self._head_hash = value
                return value
        return 'GENESIS'

    def _assert_active_key_matches_head_locked(self) -> None:
        """Reject appends when the current key cannot authenticate the log head."""
        if not os.path.exists(self.log_path):
            return
        lines = self._tail_lines(1)
        if not lines:
            return
        try:
            record = json.loads(lines[-1])
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("cannot append to an invalid audit log head") from exc
        expected_hash = str(record.get("record_hash") or "")
        if not expected_hash or not hmac.compare_digest(
            expected_hash,
            self._record_hash(record),
        ):
            raise RuntimeError(
                "active audit key does not match the current audit log head"
            )

    def _tail_lines(self, limit: int) -> list[bytes]:
        """Read at most ``limit`` lines without scanning an ever-growing log."""

        if limit <= 0 or not os.path.exists(self.log_path):
            return []
        block_size = 64 * 1024
        with open(self.log_path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count <= limit:
                read_size = min(block_size, position)
                position -= read_size
                handle.seek(position)
                block = handle.read(read_size)
                chunks.append(block)
                newline_count += block.count(b'\n')
        return b''.join(reversed(chunks)).splitlines()[-limit:]

    def _load_parsed_cache(self) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return (records_most_recent_first, full_stats), refreshing the
        mtime-keyed cache only when the log file has changed."""
        if not os.path.exists(self.log_path):
            self._cache_mtime = None
            self._cache_records = []
            self._cache_stats = {'total': 0, 'web': 0, 'cli': 0, 'api': 0, 'page_view': 0, 'errors': 0}
            return self._cache_records, self._cache_stats

        mtime = os.path.getmtime(self.log_path)
        if self._cache_mtime == mtime and self._cache_records:
            return self._cache_records, self._cache_stats

        with self._lock:
            lines = self._tail_lines(self.max_read_lines)

        records: list[dict[str, Any]] = []
        stats = {'total': 0, 'web': 0, 'cli': 0, 'api': 0, 'page_view': 0, 'errors': 0}
        for raw_line in reversed(lines):
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            record_source = record.get('source') or 'unknown'
            record_type = record.get('action_type') or 'api'
            status_code = int(record.get('status_code') or 0)

            stats['total'] += 1
            if record_source in ('web', 'cli'):
                stats[record_source] += 1
            if record_type in ('api', 'page_view'):
                stats[record_type] += 1
            if status_code >= 400:
                stats['errors'] += 1

            records.append(record)

        self._cache_mtime = mtime
        self._cache_records = records
        self._cache_stats = stats
        return records, stats

    def sanitize_value(self, key: str, value: Any) -> Any:
        if _WORD_BOUNDARY_PATTERN.search(key):
            return '***REDACTED***'
        if isinstance(value, dict):
            return self.sanitize_mapping(value)
        if isinstance(value, list):
            return [
                self.sanitize_value(key, item)
                for item in value[:50]
            ]
        if isinstance(value, str) and len(value) > 300:
            return value[:300] + '...'
        return value

    def sanitize_mapping(self, mapping: dict[str, Any] | None) -> dict[str, Any]:
        if not mapping:
            return {}
        return {
            str(key): self.sanitize_value(str(key), value)
            for key, value in mapping.items()
        }

    def summarize_json_body(self, body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception as e:
            return {
                'body_type': 'json',
                'parse_error': str(e),
                'preview': body[:300].decode('utf-8', errors='replace')
            }
        if isinstance(payload, dict):
            return {'body_type': 'json', 'data': self.sanitize_mapping(payload)}
        if isinstance(payload, list):
            return {
                'body_type': 'json',
                'data': [self.sanitize_value('items', item) for item in payload[:50]],
                'total_items': len(payload)
            }
        return {'body_type': 'json', 'data': self.sanitize_value('value', payload)}

    def summarize_form_body(self, body: bytes) -> dict[str, Any]:
        parsed = urllib.parse.parse_qs(body.decode('utf-8', errors='replace'), keep_blank_values=True)
        flattened = {
            key: values[0] if len(values) == 1 else values
            for key, values in parsed.items()
        }
        return {'body_type': 'form', 'data': self.sanitize_mapping(flattened)}

    def log_event(self, event: dict[str, Any]) -> dict[str, Any]:
        record = {
            'id': str(uuid4()),
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            **self.sanitize_mapping(event),
        }
        with self._lock, self._cross_process_lock():
            self._assert_active_key_matches_head_locked()
            self._rotate_if_needed_locked()
            # Another process may have appended since this logger instance last
            # wrote. Never trust the process-local head across the file lock.
            self._head_hash = None
            record['previous_hash'] = self._last_hash_locked()
            record['record_hash'] = self._record_hash(record)
            payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
            descriptor = os.open(
                self.log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(payload.encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.log_path, 0o600)
            self._head_hash = record['record_hash']
            self._cache_mtime = None
        return record

    def _rotate_if_needed_locked(self) -> None:
        """日志超限时保留尾部、重建 HMAC 链并备份原文件。"""
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return
        if size <= self.MAX_LOG_BYTES:
            return

        # Read the whole file, then keep only the most recent tail.
        with open(self.log_path, 'rb') as handle:
            raw = handle.read()
        lines = raw.splitlines()
        if not lines:
            return

        kept: list[dict[str, Any]] = []
        kept_bytes = 0
        for raw_line in reversed(lines):
            if kept_bytes >= self.ROTATE_KEEP_BYTES and kept:
                break
            try:
                rec = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            kept.append(rec)
            kept_bytes += len(raw_line) + 1
        kept.reverse()

        # Re-hash the retained records onto a fresh chain.
        previous = 'GENESIS'
        out_buf: list[bytes] = []
        for rec in kept:
            unsigned = {
                k: v for k, v in rec.items() if k not in ('record_hash', 'previous_hash')
            }
            unsigned['previous_hash'] = previous
            new_hash = self._record_hash(unsigned)
            unsigned['record_hash'] = new_hash
            previous = new_hash
            out_buf.append(
                json.dumps(unsigned, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            )

        rotated_path = f'{self.log_path}.rotated'
        # Preserve the original file first (don't unlink the only copy).
        # Overwrite any prior rotated backup so we don't accumulate them —
        # rotation can fire repeatedly across a long-running process.
        with open(rotated_path, 'wb') as handle:
            handle.write(raw)
        os.chmod(rotated_path, 0o600)

        descriptor = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            for line in out_buf:
                handle.write(line + b'\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.log_path, 0o600)
        # Drop the in-memory head hash: the next write re-seeds it from the
        # new tail.
        self._head_hash = previous
        self._cache_mtime = None

    def verify_chain(self) -> dict[str, Any]:
        """Verify that every record belongs to one uninterrupted signed chain."""
        if not os.path.exists(self.log_path):
            return {'valid': True, 'signed_records': 0, 'legacy_records': 0}
        previous = 'GENESIS'
        signed_records = 0
        with self._lock, self._cross_process_lock(False), open(self.log_path, encoding='utf-8') as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return {'valid': False, 'line': line_number, 'error': 'invalid JSON'}
                expected_hash = str(record.get('record_hash') or '')
                if not expected_hash:
                    return {
                        'valid': False,
                        'line': line_number,
                        'error': 'unsigned audit record',
                    }
                if str(record.get('previous_hash') or '') != previous:
                    return {'valid': False, 'line': line_number, 'error': 'previous hash mismatch'}
                actual_hash = self._record_hash(record)
                if not hmac.compare_digest(expected_hash, actual_hash):
                    return {'valid': False, 'line': line_number, 'error': 'record HMAC mismatch'}
                previous = expected_hash
                signed_records += 1
        return {
            'valid': True,
            'signed_records': signed_records,
            'legacy_records': 0,
            'head_hash': previous,
        }

    def compact_record(self, record: dict[str, Any]) -> dict[str, Any]:
        keys = (
            'id',
            'timestamp',
            'action_type',
            'source',
            'operation',
            'method',
            'path',
            'page',
            'status_code',
            'duration_ms',
            'client_ip',
            'client_id',
            'username',
            'query',
            'error',
        )
        return {key: record.get(key) for key in keys if key in record}

    def read_events(
        self,
        limit: int = 200,
        offset: int = 0,
        source: str | None = None,
        action_type: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 200), 1000))
        offset = max(0, int(offset or 0))
        empty_stats = {'total': 0, 'web': 0, 'cli': 0, 'api': 0, 'page_view': 0, 'errors': 0}

        cached_records, stats = self._load_parsed_cache()
        if not cached_records:
            return {'records': [], 'stats': empty_stats, 'offset': offset, 'limit': limit, 'has_more': False}

        query_lower = (query or '').strip().lower()
        records: list[dict[str, Any]] = []
        skipped = 0
        # 缓存记录已按时间倒序，直接在内存中过滤。
        for record in cached_records:
            record_source = record.get('source') or 'unknown'
            record_type = record.get('action_type') or 'api'

            if source and record_source != source:
                continue
            if action_type and record_type != action_type:
                continue
            if query_lower:
                haystack = ' '.join(
                    str(record.get(key, ''))
                    for key in ('username', 'client_ip', 'method', 'path', 'page', 'operation', 'user_agent')
                ).lower()
                if query_lower not in haystack:
                    continue

            if skipped < offset:
                skipped += 1
                continue
            if len(records) < limit:
                records.append(self.compact_record(record))

        return {
            'records': records,
            'stats': stats,
            'offset': offset,
            'limit': limit,
            'has_more': len(records) == limit,
        }

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        if not event_id or not os.path.exists(self.log_path):
            return None

        with self._lock:
            lines = self._tail_lines(self.max_read_lines)

        for raw_line in reversed(lines):
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if record.get('id') == event_id:
                return record
        return None


def classify_request_source(user_agent: str, path: str) -> str:
    """Classify request as browser web traffic or CLI/API tool traffic."""
    ua = (user_agent or '').lower()
    cli_markers = (
        'curl',
        'wget',
        'httpie',
        'python-requests',
        'python-urllib',
        'go-http-client',
        'java/',
        'okhttp',
        'libwww-perl',
    )
    if any(marker in ua for marker in cli_markers):
        return 'cli'
    if path.startswith('/api/') and 'mozilla' not in ua:
        return 'cli'
    return 'web'


security_audit_logger = SecurityAuditLogger()
