"""
Security audit logging for web and CLI operations.
"""
import json
import re
import os
import threading
import urllib.parse
from collections import deque
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
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
)

_WORD_BOUNDARY_PATTERN = re.compile(r'(?:' + '|'.join(re.escape(kw) for kw in SENSITIVE_KEYWORDS) + r')', re.IGNORECASE)


class SecurityAuditLogger:
    """Append-only JSONL audit log with bounded readback helpers."""

    def __init__(self, log_path: Optional[str] = None, max_read_lines: int = 5000):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.log_path = log_path or os.path.join(base_dir, 'data', 'security_audit.json')
        self.max_read_lines = max_read_lines
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

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

    def sanitize_mapping(self, mapping: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not mapping:
            return {}
        return {
            str(key): self.sanitize_value(str(key), value)
            for key, value in mapping.items()
        }

    def summarize_json_body(self, body: bytes) -> Dict[str, Any]:
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

    def summarize_form_body(self, body: bytes) -> Dict[str, Any]:
        parsed = urllib.parse.parse_qs(body.decode('utf-8', errors='replace'), keep_blank_values=True)
        flattened = {
            key: values[0] if len(values) == 1 else values
            for key, values in parsed.items()
        }
        return {'body_type': 'form', 'data': self.sanitize_mapping(flattened)}

    def log_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        record = {
            'id': str(uuid4()),
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            **event,
        }
        with self._lock:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        return record

    def compact_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
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
        source: Optional[str] = None,
        action_type: Optional[str] = None,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 200), 1000))
        records = []
        stats = {'total': 0, 'web': 0, 'cli': 0, 'api': 0, 'page_view': 0, 'errors': 0}

        if not os.path.exists(self.log_path):
            return {'records': [], 'stats': stats}

        query_lower = (query or '').strip().lower()
        with self._lock:
            with open(self.log_path, 'r', encoding='utf-8') as f:
                lines: Iterable[str] = deque(f, maxlen=self.max_read_lines)

        for line in reversed(list(lines)):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
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

            if len(records) < limit:
                records.append(self.compact_record(record))

        return {'records': records, 'stats': stats}

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        if not event_id or not os.path.exists(self.log_path):
            return None

        with self._lock:
            with open(self.log_path, 'r', encoding='utf-8') as f:
                lines: Iterable[str] = deque(f, maxlen=self.max_read_lines)

        for line in reversed(list(lines)):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
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
