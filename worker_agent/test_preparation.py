"""Idempotent preparation templates executed inside an already-fenced test job."""

from __future__ import annotations

import json
import time

from foundation.processes import run_local_command
from foundation.test_execution_options import PREPARATION_TEMPLATES, validate_execution_options


def prepare_test_devices(spec: dict, log, *, run=run_local_command, clock=time.monotonic) -> None:
    options = validate_execution_options(spec, device_count=len(spec.get('devices') or []))
    started = clock()
    for action in options['preparations']:
        for serial in spec.get('devices') or []:
            log.write('[GMS_PREP] ' + json.dumps({'action': action, 'device': serial, 'state': 'running'}) + '\n')
            log.flush()
            for command in PREPARATION_TEMPLATES[action]['commands']:
                remaining = 20 - (clock() - started)
                if remaining <= 0:
                    log.write('[GMS_PREP] ' + json.dumps({'action': action, 'device': serial, 'state': 'failed'}) + '\n')
                    log.flush()
                    raise ValueError('Device preparation exceeded its 20 second budget')
                result = run(['adb', '-s', serial, *command], timeout=max(1, min(5, int(remaining))))
                if not result.ok:
                    log.write('[GMS_PREP] ' + json.dumps({'action': action, 'device': serial, 'state': 'failed'}) + '\n')
                    log.flush()
                    raise ValueError(f'Device preparation {action} failed for {serial}')
            log.write('[GMS_PREP] ' + json.dumps({'action': action, 'device': serial, 'state': 'completed'}) + '\n')
            log.flush()
