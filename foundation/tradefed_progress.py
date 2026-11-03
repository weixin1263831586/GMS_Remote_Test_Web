"""Best-effort log telemetry, separate from authoritative archived test results."""

from __future__ import annotations

import json
import re


_START = re.compile(r'(?:starting|running|executing)\s+(?:test\s+)?module[\s:\[]+([A-Za-z0-9_.-]+)', re.I)
_MODULE = re.compile(r'(?:(arm64-v8a|armeabi-v7a|x86_64|x86)\s+)?((?:Cts|Gts|Vts|Sts)[A-Za-z0-9_.-]+)')
_COMPLETE = re.compile(r'\b(completed|finished|done)\b|\bpassed\s*[:=]', re.I)


def parse_progress_line(line: str) -> dict | None:
    line = line[:4096]
    if line.startswith('[GMS_PREP] '):
        try:
            value = json.loads(line[11:])
        except ValueError:
            return None
        if isinstance(value, dict) and value.get('state') in ('running', 'completed', 'failed'):
            return {key: str(value.get(key, ''))[:256] for key in ('action', 'device', 'state')} | {'kind': 'preparation'}
    start = _START.search(line)
    module = _MODULE.search(line)
    if not start and not module:
        return None
    if not start and not _COMPLETE.search(line) and not re.search(r'\[\d+/\d+\]', line):
        return None
    result = {'kind': 'module', 'module': start[1] if start else module[2],
              'abi': module[1] or '' if module else '', 'state': 'running' if start else 'completed'}
    for name in ('passed', 'failed'):
        match = re.search(rf'\b{name}\s*[:=]\s*(\d+)', line, re.I)
        if not match:
            match = re.search(rf'\b(\d+)\s+(?:tests?\s+)?{name}\b', line, re.I)
        if match:
            result[name] = min(int(match[1]), 10**9)
    if not start and not _COMPLETE.search(line):
        result['state'] = 'running'
    return result
