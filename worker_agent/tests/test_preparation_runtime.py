"""Preparation fails before test launch and its evidence survives that failure."""

import pytest

from worker_agent.config import WorkerConfig
from worker_agent.runtime import WorkerRuntime


def runtime_and_command(tmp_path, monkeypatch):
    suites = tmp_path / 'suites' / 'GMS-Suite'
    suites.mkdir(parents=True)
    script = suites / 'run_GMS_Test_Auto.sh'
    script.write_text('#!/bin/sh\nexit 0\n')
    runtime = WorkerRuntime(WorkerConfig(worker_id='w', controller_url='https://controller', token='test',
                                        data_root=tmp_path / 'runtime', suite_roots=[suites]))
    monkeypatch.setattr(runtime, '_ensure_test_script', lambda _: None)
    monkeypatch.setattr(runtime, '_validate_requested_module', lambda _: None)
    monkeypatch.setattr('worker_agent.runtime.subprocess.Popen', lambda *_a, **_kw: pytest.fail('must not launch'))
    command = {'id': 'command', 'job_id': 'job', 'attempt_id': 'attempt', 'payload': {
        'devices': ['w:SERIAL'], 'execution_spec': {'test_type': 'cts', 'suite_path': str(suites / 'android-cts' / 'tools'),
                                                 'devices': ['SERIAL'], 'preparations': ['wifi_on']}}}
    return runtime, command


def test_failed_preparation_is_reportable_without_starting_tradefed(tmp_path, monkeypatch):
    runtime, command = runtime_and_command(tmp_path, monkeypatch)

    def prepare(_spec, log):
        log.write('[GMS_PREP] {"action":"wifi_on","device":"SERIAL","state":"failed"}\n')
        raise ValueError('preparation failed')

    monkeypatch.setattr('worker_agent.test_preparation.prepare_test_devices', prepare)
    with pytest.raises(ValueError, match='preparation failed'):
        runtime.start_process(command)
    events = runtime.preparation_failure_events(command)
    assert events[0]['payload']['progress']['state'] == 'failed'
    assert events[0]['event_type'] == 'module_progress'


def test_preparation_rejects_device_outside_job_fence(tmp_path, monkeypatch):
    runtime, command = runtime_and_command(tmp_path, monkeypatch)
    command['payload']['devices'] = ['w:OTHER']
    monkeypatch.setattr('worker_agent.test_preparation.prepare_test_devices', lambda *_: pytest.fail('outside fence'))
    with pytest.raises(ValueError, match='fenced job devices'):
        runtime.start_process(command)
    assert runtime.preparation_failure_events(command) == []
